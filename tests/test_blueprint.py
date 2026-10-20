"""Exercise the actual shipped Jinja reducer, not a separate Python algorithm."""
import ast
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import unittest

import yaml
from jinja2 import StrictUndefined, Undefined
from jinja2.nativetypes import NativeEnvironment

ROOT=Path(__file__).resolve().parents[1]
TZ=timezone(timedelta(hours=2))

class Loader(yaml.SafeLoader):
    pass
Loader.add_constructor('!input',lambda loader,node: {'input':loader.construct_scalar(node)})

AUTOMATION=yaml.load((ROOT/'blueprints/automation/cold_water_temperature.yaml').read_text(encoding='utf-8'),Loader=Loader)
REDUCER=next(a['variables']['next_state'] for a in AUTOMATION['actions'] if 'next_state' in a.get('variables',{}))
SENSORS=[yaml.load(p.read_text(encoding='utf-8'),Loader=Loader)['sensor'] for p in sorted((ROOT/'blueprints/template').glob('*.yaml'))]


def number(value):
    try:
        return math.isfinite(float(value))
    except (TypeError,ValueError):
        return False


def as_datetime(value):
    if isinstance(value,datetime):
        return value
    if isinstance(value,(float,int)):
        return datetime.fromtimestamp(value,timezone.utc)
    return datetime.fromisoformat(str(value).replace('Z','+00:00'))


def as_timestamp(value,default=None):
    try:
        return as_datetime(value).timestamp()
    except (TypeError,ValueError,OverflowError):
        return default


def from_json(value,default=None):
    try:
        return json.loads(value)
    except (TypeError,ValueError):
        return default


class States:
    def __init__(self):
        self.data={}
    def __call__(self,key):
        obj=self.data.get(key)
        return obj.state if obj else 'unknown'
    def __getitem__(self,key):
        return self.data.get(key)
    def set(self,key,value,t):
        self.data[key]=SimpleNamespace(state=str(value),last_updated=as_datetime(t))


class Harness:
    def __init__(self,heartbeat=True,mode='strict',guard=False):
        self.t=int(datetime(2026,10,20,10,tzinfo=TZ).timestamp())
        self.states=States()
        self.states.set('input_text.state','',self.t)
        self.states.set('sensor.temperature','16',self.t)
        self.states.set('sensor.volume','1',self.t)
        self.states.set('sensor.received',as_datetime(self.t).isoformat(),self.t)
        self.variables={'temperature_entity':'sensor.temperature','volume_entity':'sensor.volume',
                        'helper_entity':'input_text.state','receipt_entity':'sensor.received' if heartbeat else [],
                        'volume_factor':1000,'flush':5,'tolerance':.2,'span':90,'pause':150,'timeout':180,'fresh_hours':24,
                        'sampling_mode':mode,'confirmation_span':30,
                        'idle_hours':3 if guard else 0,'guarded_flush':10,'guarded_wait':120,'guarded_span':30}
        self.env=NativeEnvironment(undefined=StrictUndefined)
        self.env.filters.update(from_json=from_json,to_json=lambda v: json.dumps(v,separators=(',',':')))
        self.env.globals.update(states=self.states,now=lambda:as_datetime(self.t).astimezone(TZ),
                                as_timestamp=as_timestamp,as_datetime=as_datetime,
                                as_local=lambda v:as_datetime(v).astimezone(TZ),is_number=number)
        self.template=self.env.from_string(REDUCER)

    def run(self,event='reading'):
        result=self.template.render(**self.variables,event_kind=event)
        if isinstance(result,str):
            result=ast.literal_eval(result.strip())
        self.encoded=json.dumps(result,separators=(',',':'))
        if len(self.encoded)>255:
            raise AssertionError(f'Packed state too long: {len(self.encoded)} {self.encoded}')
        self.states.set('input_text.state',self.encoded,self.t)
        self.s=result
        return result

    def advance(self,seconds,volume=None,temperature=None,receipt=True,event='reading'):
        self.t+=seconds
        if volume is not None:
            self.states.set('sensor.volume',volume,self.t)
        if temperature is not None:
            self.states.set('sensor.temperature',temperature,self.t)
        if receipt:
            self.states.set('sensor.received',as_datetime(self.t).isoformat(),self.t)
        return self.run(event)

    def establish(self):
        self.run()
        self.advance(20)

    def qualify(self):
        self.establish()
        self.advance(60,volume=1.003,temperature=14.2)
        self.advance(60,volume=1.005,temperature=13.9)
        self.advance(60,volume=1.008,temperature=13.8)
        return self.advance(60,volume=1.011,temperature=13.9)

    def qualify_guard(self):
        self.establish()
        self.advance(60,volume=1.006,temperature=13.5)
        self.advance(60,volume=1.010,temperature=13.6)
        self.advance(60,volume=1.011,temperature=13.8)
        self.advance(20,event='packet')
        return self.advance(20,event='packet')

    def idle_for(self,seconds):
        while seconds > 0:
            step=min(seconds,120)
            self.advance(step,event='packet')
            seconds-=step


class BlueprintTests(unittest.TestCase):
    def test_guard_excludes_cold_dip_and_needs_flow_after_wait(self):
        h=Harness(mode='batch',guard=True);h.establish()
        h.advance(60,volume=1.006,temperature=13.5)
        h.advance(60,volume=1.010,temperature=13.6)
        self.assertEqual(h.s['q'],[])
        for _ in range(4):
            h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[])
        self.assertEqual(h.s['d'][2],0)
        h.advance(20,volume=1.012,temperature=13.8)
        anchor=h.t
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[anchor,13.8])
        self.assertEqual(h.s['d'],[20261020,13.8,1])
        self.assertEqual(h.s['h'],0)

    def test_guard_stays_armed_through_small_draws(self):
        h=Harness(mode='batch',guard=True);h.establish()
        h.advance(60,volume=1.002,temperature=13.5)
        h.advance(160,event='packet')
        self.assertEqual(h.s['h'],1)
        h.advance(20,volume=1.010,temperature=13.5)
        h.advance(60,volume=1.012,temperature=13.6)
        self.assertEqual(h.s['a'],[])
        self.assertEqual(h.s['q'],[])
        h.advance(60,volume=1.013,temperature=13.8)
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'][1],13.8)
        self.assertEqual(h.s['h'],0)

    def test_guard_session_pause_resets_elapsed_time_and_volume(self):
        h=Harness(mode='batch',guard=True);h.establish()
        h.advance(60,volume=1.006,temperature=13.5)
        h.advance(151,event='packet')
        h.advance(20,volume=1.011,temperature=13.8)
        self.assertEqual(h.s['l'],5)
        self.assertEqual(h.s['h'],h.t)
        h.advance(40,event='packet')
        self.assertEqual(h.s['a'],[])

    def test_guard_release_restores_normal_acceptance_for_later_draws(self):
        h=Harness(mode='batch',guard=True);h.qualify_guard()
        self.assertEqual(h.s['h'],0)
        h.advance(160,event='packet')
        last_flow=h.s['f']
        self.assertGreater(last_flow,0)
        h.advance(20,volume=1.016,temperature=14)
        anchor=h.t
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[anchor,14])

    def test_idle_guard_arms_at_configured_boundary_despite_healthy_packets(self):
        h=Harness(mode='batch',guard=True);h.qualify_guard()
        last_flow=h.s['f']
        h.idle_for(last_flow+10800-1-h.t)
        self.assertEqual(h.s['h'],0)
        self.assertEqual(h.s['f'],last_flow)
        self.assertEqual(h.s['b'],1)
        h.advance(1,event='packet')
        self.assertEqual(h.s['h'],1)
        self.assertEqual(h.s['f'],last_flow)
        h.advance(20,volume=1.016,temperature=13)
        self.assertGreater(h.s['h'],1)
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'][1],13.8)

    def test_all_guard_thresholds_are_configurable(self):
        h=Harness(mode='batch',guard=True)
        h.variables.update(idle_hours=1,guarded_flush=15,guarded_wait=180,guarded_span=60)
        h.establish()
        for seconds,volume in [(60,1.006),(60,1.012),(60,1.014)]:
            h.advance(seconds,volume=volume,temperature=13.5)
            self.assertEqual(h.s['q'],[])
        h.advance(60,volume=1.015,temperature=14)
        anchor=h.t
        h.advance(30,event='packet')
        self.assertEqual(h.s['a'],[])
        h.advance(30,event='packet')
        self.assertEqual(h.s['a'],[anchor,14])
        last_flow=h.s['f']
        h.idle_for(last_flow+3600-h.t)
        self.assertEqual(h.s['h'],1)

    def test_guard_can_be_disabled(self):
        h=Harness(mode='batch',guard=True);h.variables['idle_hours']=0
        h.establish();h.advance(60,volume=1.005,temperature=14)
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'][1],14)
        self.assertEqual(h.s['h'],0)

    def test_guard_never_reduces_normal_flush_requirement(self):
        h=Harness(mode='batch',guard=True)
        h.variables.update(flush=20,guarded_flush=10,guarded_wait=0)
        h.establish();h.advance(60,volume=1.015,temperature=14)
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[])
        h.advance(20,volume=1.020,temperature=14)
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'][1],14)

    def test_guard_works_with_strict_flow_samples(self):
        h=Harness(guard=True);h.establish()
        h.advance(60,volume=1.006,temperature=13.5)
        h.advance(60,volume=1.010,temperature=13.6)
        self.assertEqual(h.s['q'],[])
        for volume in [1.011,1.012,1.013]:
            h.advance(60,volume=volume,temperature=13.8)
        self.assertEqual(h.s['a'][1],13.8)
        self.assertEqual(h.s['h'],0)

    def test_guard_restart_preserves_accepted_values_and_rearms(self):
        h=Harness(mode='batch',guard=True);h.qualify_guard()
        accepted=h.s['a'][:];daily=h.s['d'][:]
        h.advance(1,event='reset')
        self.assertEqual(h.s['h'],1)
        self.assertEqual(h.s['a'],accepted)
        self.assertEqual(h.s['d'],daily)
        h.advance(20,volume=1.030,event='packet')
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'],accepted)
        self.assertEqual(h.s['l'],0)

    def test_v11_migration_preserves_results_and_arms_guard(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.008,temperature=13.5)
        h.advance(20,event='packet');h.advance(20,event='packet')
        legacy=h.s.copy();legacy.pop('h');legacy['c']=legacy['c'][:8]
        h.states.set('input_text.state',json.dumps(legacy),h.t)
        h.variables['idle_hours']=3
        h.run('tick')
        self.assertEqual(h.s['h'],1)
        self.assertEqual(h.s['b'],0)
        self.assertEqual(h.s['a'],legacy['a'])
        self.assertEqual(h.s['d'],legacy['d'])

    def test_guard_reception_gap_discards_jump_and_early_candidates(self):
        h=Harness(mode='batch',guard=True);h.qualify_guard()
        accepted=h.s['a'][:]
        h.advance(181,volume=1.040,event='packet')
        self.assertEqual(h.s['h'],1)
        self.assertEqual(h.s['l'],0)
        h.advance(20,volume=1.045,temperature=13)
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'],accepted)

    def test_guard_pending_state_with_accepted_value_fits_text_helper(self):
        for mode in ['batch','strict']:
            with self.subTest(mode=mode):
                h=Harness(mode='batch',guard=True);h.qualify_guard()
                h.variables['sampling_mode']=mode
                h.run('reset');h.advance(20)
                h.advance(60,volume=123456789.005,temperature=14)
                for offset,temp in [(60,14),(60,14),(60,14.4),(60,14.8),(60,15.2)]:
                    h.advance(offset,volume=float(h.states('sensor.volume'))+.005,temperature=temp)
                self.assertLessEqual(len(h.encoded),255)

    def test_sources_are_entities_and_no_mqtt_dependency(self):
        serialized=json.dumps(AUTOMATION)
        self.assertNotIn('mqtt.publish',serialized)
        self.assertFalse(any(t['trigger']=='mqtt' for t in AUTOMATION['triggers']))
        self.assertEqual(AUTOMATION['mode'],'queued')
        self.assertTrue(all('selector' in v for section in AUTOMATION['blueprint']['input'].values() for v in section['input'].values()))
        self.assertTrue(any(t.get('id')=='packet' and t.get('event_data',{}).get('entity_id')=={'input':'receipt_sensor'}
                            for t in AUTOMATION['triggers']))

    def test_batch_two_coarse_increases_need_no_further_volume_change(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.003,temperature=13.5)
        self.assertEqual(h.s['q'],[])
        h.advance(82,volume=1.011,temperature=13.6)
        flow_time=h.t
        self.assertEqual(h.s['a'],[])
        h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[])
        h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[flow_time,13.6])
        self.assertEqual(h.s['d'][2],1)
        for _ in range(8):
            h.advance(20,event='packet')
        self.assertEqual(h.s['d'][2],1)

    def test_single_flushed_batch_can_qualify(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.008,temperature=14)
        flow_time=h.t
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[flow_time,14])

    def test_idle_or_insufficient_volume_packets_cannot_start_batch(self):
        for volume in [None,1.003]:
            with self.subTest(volume=volume):
                h=Harness(mode='batch');h.establish()
                h.advance(60,volume=volume,temperature=14)
                for _ in range(10):
                    h.advance(20,event='packet')
                self.assertEqual(h.s['a'],[])

    def test_duplicate_runs_without_new_receipt_cannot_confirm(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.008,temperature=14)
        for _ in range(5):
            h.advance(20,receipt=False,event='tick')
        self.assertEqual(h.s['q'][3],1)
        self.assertEqual(h.s['a'],[])

    def test_rejected_batch_cannot_slide_into_later_idle_plateau(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.008,temperature=14)
        h.advance(20,temperature=14.3,event='packet')
        self.assertEqual(h.s['q'],[])
        for _ in range(5):
            h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[])
        h.advance(20,volume=1.011,temperature=14)
        flow_time=h.t
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[flow_time,14])

    def test_batch_publishes_flow_temperature_not_later_warming(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.008,temperature=14)
        flow_time=h.t
        h.advance(20,temperature=14.1,event='packet')
        h.advance(20,temperature=14.2,event='packet')
        self.assertEqual(h.s['a'],[flow_time,14])

    def test_batch_keeps_extrema_between_spaced_confirmation_points(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.008,temperature=14)
        h.advance(5,temperature=14.2,event='packet')
        self.assertEqual(h.s['q'][3],1)
        h.advance(15,temperature=13.9,event='packet')
        self.assertEqual(h.s['q'],[])
        self.assertEqual(h.s['a'],[])

    def test_counter_reset_cancels_pending_batch(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.008,temperature=14)
        h.advance(20,volume=.001,event='packet')
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[])

    def test_batch_confirmed_after_midnight_keeps_original_measurement_date(self):
        h=Harness(mode='batch');h.establish()
        h.t=int(datetime(2026,10,20,23,59,30,tzinfo=TZ).timestamp())
        h.states.set('sensor.received',as_datetime(h.t).isoformat(),h.t)
        h.run()
        h.advance(20,volume=1.008,temperature=14)
        flow_time=h.t
        h.advance(20,event='packet');h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[flow_time,14])
        self.assertEqual(h.s['d'],[20261021,None,0])

    def test_continued_fine_flow_does_not_restart_confirmation(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.005,temperature=14)
        h.advance(20,volume=1.006,temperature=14)
        h.advance(20,volume=1.007,temperature=14)
        self.assertEqual(h.s['a'][1],14)

    def test_batch_reception_gap_discards_confirmation_and_volume_jump(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.008,temperature=14)
        h.advance(181,volume=1.040,temperature=14)
        for _ in range(3):
            h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[])
        self.assertEqual(h.s['l'],0)

    def test_batch_flow_pause_prevents_late_confirmation(self):
        h=Harness(mode='batch');h.establish()
        h.advance(60,volume=1.008,temperature=14)
        h.advance(151,event='packet')
        for _ in range(3):
            h.advance(20,event='packet')
        self.assertEqual(h.s['a'],[])

    def test_migration_preserves_accepted_value_and_resets_pending_samples(self):
        h=Harness();h.qualify();accepted=h.s['a'][:]
        h.variables['sampling_mode']='batch'
        h.run('tick')
        self.assertEqual(h.s['a'],accepted)
        self.assertEqual(h.s['b'],0)
        self.assertEqual(h.s['q'],[])

    def test_flush_and_stable_median(self):
        h=Harness();s=h.qualify()
        self.assertEqual(s['a'],[h.t,13.9])
        self.assertEqual(s['d'],[20261020,13.9,1])
        self.assertLessEqual(len(h.encoded),255)

    def test_idle_messages_do_not_count_or_repeat_acceptance(self):
        h=Harness();h.qualify();a=h.s['a'][:]
        for _ in range(12):
            h.advance(20,event='tick')
        self.assertEqual(h.s['a'],a)
        self.assertEqual(h.s['d'][2],1)
        self.assertEqual(h.s['b'],1)
        self.assertEqual(h.s['l'],0)

    def test_receiver_gap_discards_volume_jump(self):
        h=Harness();h.establish()
        h.advance(240,volume=1.050,temperature=14)
        self.assertEqual(h.s['p'],1050)
        self.assertEqual(h.s['l'],0)
        h.advance(60,volume=1.053,temperature=14)
        self.assertEqual(h.s['l'],3)

    def test_expired_heartbeat_cannot_add_new_volume(self):
        h=Harness();h.establish()
        h.advance(181,volume=1.050,receipt=False,event='tick')
        self.assertEqual(h.s['b'],0)
        self.assertEqual(h.s['l'],0)
        h.advance(20,volume=1.060)
        self.assertEqual(h.s['p'],1060)
        self.assertEqual(h.s['l'],0)

    def test_counter_reset_is_not_negative_flow(self):
        h=Harness();h.qualify();a=h.s['a'][:]
        h.advance(60,volume=.001,temperature=14)
        self.assertEqual(h.s['l'],0)
        self.assertEqual(h.s['q'],[])
        self.assertEqual(h.s['a'],a)

    def test_restart_preserves_result_but_requires_new_observation(self):
        h=Harness();h.qualify();a=h.s['a'][:]
        h.advance(1,receipt=False,event='reset')
        self.assertEqual(h.s['b'],0)
        self.assertEqual(h.s['a'],a)
        h.advance(1,receipt=False,event='tick')
        self.assertEqual(h.s['b'],0)
        h.advance(20,volume=1.030)
        self.assertEqual(h.s['b'],1)
        self.assertEqual(h.s['l'],0)

    def test_midnight_resets_daily_but_not_last_measurement(self):
        h=Harness();h.qualify();a=h.s['a'][:]
        h.t=int(datetime(2026,10,21,0,tzinfo=TZ).timestamp())
        h.run('midnight')
        self.assertEqual(h.s['a'],a)
        self.assertEqual(h.s['d'],[20261021,None,0])

    def test_brief_unavailable_event_invalidates_even_after_recovery(self):
        h=Harness();h.qualify();a=h.s['a'][:]
        h.advance(2,event='invalid')
        self.assertEqual(h.s['b'],0)
        self.assertEqual(h.s['a'],a)

    def test_fallback_temperature_updates_keep_volume_plateau_continuous(self):
        h=Harness(heartbeat=False);h.run()
        h.advance(20,temperature=16.1)
        for value in [16.2,16.3,16.4,16.5]:
            h.advance(60,temperature=value)
        self.assertEqual(h.s['b'],1)
        h.advance(60,volume=1.003,temperature=14)
        self.assertEqual(h.s['l'],3)

    def test_unstable_window_is_rejected_and_later_settles(self):
        h=Harness();h.establish()
        for volume,temp in [(1.005,15),(1.008,14),(1.011,13.9)]:
            h.advance(60,volume=volume,temperature=temp)
        self.assertEqual(h.s['a'],[])
        h.advance(60,volume=1.014,temperature=14)
        self.assertEqual(h.s['a'][1],14)

    def test_dense_updates_can_settle(self):
        h=Harness();h.establish()
        for i in range(1,12):
            h.advance(20,volume=1+i*.005,temperature=14)
        self.assertEqual(h.s['a'][1],14)

    def test_invalid_json_and_wrong_candidate_type_recover(self):
        for raw in ['not json','[]','{"v":1,"q":{"x":1}}']:
            with self.subTest(raw=raw):
                h=Harness();h.states.set('input_text.state',raw,h.t);h.run()
                self.assertEqual(h.s['b'],0)
                self.assertEqual(h.s['a'],[])

    def test_display_templates_compile_and_render(self):
        h=Harness();h.qualify()
        def render(template):
            value=h.env.from_string(template).render(helper_entity='input_text.state')
            if isinstance(value,str):
                try:
                    return ast.literal_eval(value.strip())
                except (SyntaxError,ValueError):
                    return value.strip()
            return value
        for sensor in SENSORS:
            self.assertEqual(render(sensor['state']),13.9)
            self.assertTrue(render(sensor['availability']))
            for template in sensor['attributes'].values():
                render(template)
        h.t+=86400
        self.assertFalse(render(SENSORS[1]['availability']))


if __name__=='__main__':
    unittest.main()
