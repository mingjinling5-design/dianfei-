import unittest
from unittest.mock import Mock
from ysp.eschool_service import resolve_room


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.fields=dict(areaId='1',buildingCode='2',floorCode='3',roomCode='0101')
        self.room=dict(area_id='1',building_code='2',floor_code='3',room_code='0101')
        self.responses={
            'queryBind':{'rows':[self.fields]},
            'queryArea':{'rows':[{'id':'1','areaName':'校区'}]},
            'queryBuilding':{'rows':[{'buildingCode':'2','buildingName':'楼栋'}]},
            'queryFloor':{'rows':[{'floorCode':'3','floorName':'楼层'}]},
            'queryRoom':{'rows':[{'roomCode':'0101','roomName':'0101室'}]}}
        self.client=Mock()
        self.client.electric.side_effect=lambda name,params:self.responses[name]

    def test_bound_room_is_filled_with_labels(self):
        result=resolve_room(self.client,{'rooms':[]})
        self.assertTrue(result['restored'])
        self.assertEqual(result['selection']['roomCode'],'0101')
        self.assertEqual(result['selection']['areaName'],'校区')
        self.assertEqual(len(result['options']),4)

    def test_saved_room_is_restored_when_no_binding(self):
        self.responses['queryBind']['rows']=[]
        self.assertTrue(resolve_room(self.client,{'rooms':[self.room]})['restored'])

    def test_multiple_rooms_are_not_arbitrarily_selected(self):
        self.responses['queryBind']['rows'].append({**self.fields,'roomCode':'0102'})
        self.assertFalse(resolve_room(self.client,{'rooms':[]})['restored'])

    def test_missing_history_requires_selection(self):
        self.responses['queryBind']['rows']=[]
        self.assertFalse(resolve_room(self.client,{'rooms':[]})['restored'])

    def test_invalid_saved_room_is_not_restored(self):
        self.responses['queryBind']['rows']=[]
        self.responses['queryRoom']['rows']=[]
        self.assertFalse(resolve_room(self.client,{'rooms':[self.room]})['restored'])

if __name__=='__main__':unittest.main()
