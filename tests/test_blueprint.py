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
    def __init__(self,heartbeat=True):
        self.t=int(datetime(2026,10,20,10,tzinfo=TZ).timestamp())
        self.states=States()
        self.states.set('input_text.state','',self.t)
        self.states.set('sensor.temperature','16',self.t)
        self.states.set('sensor.volume','1',self.t)
        self.states.set('sensor.received',as_datetime(self.t).isoformat(),self.t)
        self.variables={'temperature_entity':'sensor.temperature','volume_entity':'sensor.volume',
                        'helper_entity':'input_text.state','receipt_entity':'sensor.received' if heartbeat else [],
                        'volume_factor':1000,'flush':5,'tolerance':.2,'span':90,'pause':150,'timeout':180,'fresh_hours':24}
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


class BlueprintTests(unittest.TestCase):
    def test_sources_are_entities_and_no_mqtt_dependency(self):
        serialized=json.dumps(AUTOMATION)
        self.assertNotIn('mqtt.publish',serialized)
        self.assertFalse(any(t['trigger']=='mqtt' for t in AUTOMATION['triggers']))
        self.assertEqual(AUTOMATION['mode'],'queued')
        self.assertTrue(all('selector' in v for section in AUTOMATION['blueprint']['input'].values() for v in section['input'].values()))

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
