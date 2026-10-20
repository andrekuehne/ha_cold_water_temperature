from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]
class Loader(yaml.SafeLoader):
    pass
Loader.add_constructor('!input',lambda loader,node: {'input':loader.construct_scalar(node)})
sensors=[]
for filename,name,unique_id in [('cold_water_temperature.yaml','Incoming water temperature','incoming_water_temperature'),
                                 ('cold_water_temperature_today.yaml','Incoming water temperature today','incoming_water_temperature_today')]:
    data=yaml.load((ROOT/'blueprints/template'/filename).read_text(encoding='utf-8'),Loader=Loader)
    sensor=data['sensor']
    sensor['name']=name
    sensor['unique_id']=unique_id
    sensors.append(sensor)
package={'input_text':{'incoming_water_state':{'name':'Incoming water state','max':255,'icon':'mdi:water-thermometer'}},
         'template':[{'variables':{'helper_entity':'input_text.incoming_water_state'},'sensor':sensors}]}
(ROOT/'examples/package.yaml').write_text('# No initial value: restore the last accepted result across restarts.\n'+yaml.safe_dump(package,sort_keys=False,allow_unicode=True,width=100),encoding='utf-8')
(ROOT/'examples/sensors_only.yaml').write_text(yaml.safe_dump(package['template'],sort_keys=False,allow_unicode=True,width=100),encoding='utf-8')
