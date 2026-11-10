import copy
import json
from pathlib import Path
import tempfile
import unittest

from import_database import Compiler, START, END, DAYS, compile_captures, publication_blockers, integration_report
from audit_database import audit


PREFIX = 'eurocontrol.nconect.airspace.em.rs.'


def point(name):
    return {'@class': PREFIX + 'RoutingElementPoint',
            'id': {'ref': {'pointId': name, 'backendId': name}}}


def fixture():
    summary = {'ref': {'restrictionId': 'TEST', 'backendId': 'TEST:2'},
               'flightPlanProcessing': 'HARD_SOFT'}
    state = {
        '@class': PREFIX + 'HSRestrictionState', 'stateId': 'v1',
        'period': {'start': START, 'end': END}, 'enabled': 'Enabled_TRUE',
        'condition': {'@class': PREFIX + 'HSPTRestrictionCondition',
                      'includedLocation': {'elements': [{
                          '@class': PREFIX + 'LocationConditionGroupElementCrossing',
                          'referenceLocation': True,
                          'crossingLocation': [dict(point('A'), **{'@class': PREFIX + 'CrossingLocationElementPoint'})]}]}},
        'routing': {'sequences': [{'elements': [point('B'), point('C')]}]},
        'restrictionType': 'MANDATORY', 'duringApplicability': True,
        'applicabilities': [{
            'weeklyDayPeriodPattern': {'timeFrame': {'start': START, 'end': END},
                'timePattern': [{'@class': 'eurocontrol.nconect.airspace.em.common.time.WeeklyDayPeriod',
                                 'weekDays': DAYS, 'wef': 0, 'til': 0}]},
            'altitudeRange': {'min': {'LIMIT': 'MIN_NOT_DEFINED'}, 'max': {'LIMIT': 'MAX_NOT_DEFINED'}}}],
    }
    return {'schema': 'eurocontrol-rs-capture/1', 'branch': 'production',
            'referenceStart': START, 'expectedTotal': 1,
            'index': [{'RESTRICTION': summary}],
            'records': [{'index': 0, 'summary': summary,
                         'data': {'@class': PREFIX + 'HSRestriction', 'ref': summary['ref'], 'states': [state]}}]}


class DatabaseTests(unittest.TestCase):
    def test_enroute_dct_keeps_distance_exceptions_and_forbidden_mode(self):
        data=fixture();s=data['records'][0]['data']['states'][0];s['@class']=PREFIX+'DctLimitRestrictionState'
        s['condition']={'@class':PREFIX+'CrossingDctLimitRestrictionCondition','dctLimit':50,'airspaceId':{'ref':{'airspaceId':'AREA'}}}
        s.pop('routing')
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['clauses'][0]['condition']['limitNm'],50)
        self.assertEqual(rule['referenceLocation']['op'],'dctArea')
        s['restrictionType']='FORBIDDEN';s['condition'].pop('dctLimit')
        self.assertTrue(self.compile(data)['rules'][0]['conversionDiagnostics'])
        s['routing']={'sequences':[{'elements':[{'@class':PREFIX+'RoutingElementDctSegment','positionedObject1':{'ref':{'pointId':'A'}},'positionedObject2':{'ref':{'pointId':'B'}}}]}]}
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['clauses'][0]['condition']['mode'],'FORBIDDEN')

    def test_fra_numeric_bands_and_military_exception(self):
        record=json.loads(Path(__file__).with_name('test-fra-biegpx500a.json').read_text(encoding='utf-8'))
        s=record['data']['states'][0]
        band={'min':{'discriminator':'FL','FL':195},'max':{'discriminator':'FL','FL':245}}
        s['condition']['verticalLimits']=band
        s['condition']['condition']={'@class':PREFIX+'GroupCondition','operator':'NOT','conditions':[{'@class':PREFIX+'FlightTypeCondition','value':'MILITARY'}]}
        for q in s['routing']['sequences']:q['elements'][0]['verticalLimits']=band
        rule=Compiler().state(record,s,0)
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['scope'],{'op':'not','args':[{'op':'flightType','values':['M']}]})
        self.assertEqual(rule['referenceLocation']['crossingBand'],{'minFt':19500,'maxFt':24500})
        self.assertEqual(len(rule['clauses'][0]['condition']['pointOptions']),7)
        self.assertEqual(rule['clauses'][0]['condition']['entryExitPoints'],[])
        s['@class']=PREFIX+'DctLimitRestrictionState'
        s['routing']={'sequences':[{'elements':[{'@class':PREFIX+'RoutingElementDctSegment',
            'positionedObject1':{'ref':{'pointId':'AAA'}},'positionedObject2':{'ref':{'pointId':'BBB'}},'verticalLimits':band}]}]}
        self.assertEqual(Compiler().state(record,s,0)['conversionDiagnostics'],[])

    def test_ptr_nonterminal_disjunction_does_not_erase_required_departure(self):
        c=Compiler()
        departure={'@class':PREFIX+'LocationConditionGroupElementTerminalDeparting','terminalLocation':[{'id':{'ref':{'icaoId':'EBBR'}}}]}
        crossing={'@class':PREFIX+'LocationConditionGroupElementCrossing','crossingLocation':[dict(point('REMBA'),**{'@class':PREFIX+'CrossingLocationElementPoint'})]}
        condition={'includedLocation':{'elements':[departure]},'logicalOperandLocation':'AND',
            'secondLocation':{'elements':[crossing,dict(crossing,operand='OR')]}}
        self.assertEqual(c.ptr_terminal_extensions(condition),[{'terminal':'dep','condition':{'op':'dep','values':['EBBR']}}])
        condition['secondLocation']['elements'][1]=dict(departure,operand='OR')
        self.assertIsNone(c.ptr_terminal_extensions(condition))

    def test_real_biegpx500a_source_is_fully_translated_without_loss(self):
        record=json.loads(Path(__file__).with_name('test-fra-biegpx500a.json').read_text(encoding='utf-8'))
        source=copy.deepcopy(record['data']['states'][0])
        rule=Compiler().state(record,source,0)
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['sourceState'],record['data']['states'][0])
        predicate=rule['clauses'][0]['condition']
        self.assertEqual(predicate['op'],'fraBorderViolation')
        self.assertEqual(predicate['entryExitPoints'],['MATIK','NALAN','PEMOS','RATSU','RIXUN','SOSAR','OSBON'])
        self.assertEqual(len(predicate['borders']),2)

    def test_fra_border_entry_exit_is_correlated_and_strict(self):
        data=fixture();s=data['records'][0]['data']['states'][0]
        s['@class']=PREFIX+'FraDctLimitRestrictionState'
        s['condition']={'@class':PREFIX+'CrossingBorderDctLimitRestrictionCondition','dctLimit':0,
            'crossingBorderAirspaceIds':[{'fromAirspaceId':{'ref':{'airspaceId':'A'}},'toAirspaceId':{'ref':{'airspaceId':'B'}}}]}
        s['routing']={'sequences':[{'elements':[dict(point('MATIK'),fraType='ENTRY_EXIT')]}]}
        s['enableFraPtGeneration']=False
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['referenceLocation'],{'op':'fraBorder','borders':[{'from':'A','to':'B'}]})
        self.assertEqual(rule['clauses'][0]['condition']['op'],'fraBorderViolation')
        self.assertEqual(rule['clauses'][0]['condition']['sourceValidity'],s['period'])
        s['routing']['sequences'][0]['elements'][0]['fraType']='ENTRY'
        self.assertTrue(self.compile(data)['rules'][0]['conversionDiagnostics'])
        s['routing']['sequences'][0]['elements'][0]['fraType']='ENTRY_EXIT'
        s['enableFraPtGeneration']=True
        self.assertTrue(self.compile(data)['rules'][0]['conversionDiagnostics'])

    def test_multiple_point_ptrs_keep_alternatives_and_reference_times(self):
        data=fixture();s=data['records'][0]['data']['states'][0]
        s['@class']=PREFIX+'PTRestrictionState';s['restrictionType']='MANDATORY'
        refs=s['condition']['includedLocation']['elements'][0]['crossingLocation']
        refs.append(dict(point('B'),**{'@class':PREFIX+'CrossingLocationElementPoint'}))
        es=[]
        for name in ['A','B']:
            e=point(name);e['verticalLimits']={'min':{'discriminator':'FL','FL':220},'max':{'discriminator':'FL','FL':220}};es.append({'elements':[e]})
        s['routing']={'sequences':es};r=self.compile(data)['rules'][0]
        self.assertEqual(r['conversionDiagnostics'],[]);g=r['profileAdjustments'][0]
        self.assertEqual(g['mode'],'MANDATORY');self.assertEqual(len(g['profileChoices']),2)
        self.assertEqual([c['referenceLocation']['values'] for c in g['profileChoices']],[['A'],['B']])
        s['restrictionType']='CLOSED';s.pop('routing')
        for r in refs:r['verticalLimits']={'min':{'discriminator':'FL','FL':210},'max':{'discriminator':'LIMIT','LIMIT':'UNLIMITED'}}
        r=self.compile(data)['rules'][0];self.assertEqual(r['conversionDiagnostics'],[])
        self.assertEqual(r['profileAdjustments'][0]['mode'],'FORBIDDEN')

    def test_native_route_availability_retains_identity_band_and_offsets(self):
        c=Compiler();v={'@class':PREFIX+'DependentApplicabilityRouteAvailability',
            'route':{'ref':{'backendId':'R1','uuid':'r','icaoId':'R1'}},
            'fromPoint':{'ref':{'backendId':'A','uuid':'a','pointId':'A'}},
            'toPoint':{'ref':{'backendId':'B','uuid':'b','pointId':'B'}},
            'offsetWef':-5,'offsetTil':10,'verticalLimits':{
                'min':{'discriminator':'FL','FL':195},'max':{'discriminator':'FL','FL':245}}}
        n=c.dependent_condition(v,{})
        self.assertEqual(n['op'],'nativeRouteAvailability');self.assertEqual(n['contextKey'],'R1|A|B')
        self.assertEqual((n['minFt'],n['maxFt']),(19500,24500));self.assertEqual(n['offsetStartMinutes'],-5)
        v['newFlag']=True;self.assertEqual(c.dependent_condition(v,{})['op'],'unknown')

    def test_confirmed_restriction_activations_are_half_open_intervals(self):
        c=Compiler();v={'@class':PREFIX+'DependentApplicabilityRestrictionActivation','activations':[
            {'startTime':START,'endTime':START+7000,'confirmed':'SAVED_CONFIRMED','remark':'','fmp':''}]}
        self.assertEqual(c.dependent_condition(v,{}),{'op':'utcInterval','start':START,'end':START+7000})
        v['activations'][0]['confirmed']='DRAFT'
        self.assertEqual(c.dependent_condition(v,{})['op'],'unknown')

    def test_forbidden_airspace_ptr_preserves_native_block_bounds(self):
        data=fixture();s=data['records'][0]['data']['states'][0]
        s['@class']=PREFIX+'PTRestrictionState';s['restrictionType']='FORBIDDEN'
        e={'@class':PREFIX+'RoutingElementAirspace','id':{'ref':{'airspaceId':'TEST'}}}
        s['routing']={'sequences':[{'elements':[e]}]}
        s['condition']['includedLocation']['elements'][0]['crossingLocation']=[dict(e,**{'@class':PREFIX+'CrossingLocationElementAirspace'})]
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['profileAdjustments'],[{'airspace':'TEST','forbiddenAirspaceBand':{'min':'FLOOR','max':'CEILING'}}])
        s['restrictionType']='MANDATORY'
        mandatory=self.compile(data)['rules'][0]
        self.assertEqual(mandatory['conversionDiagnostics'],[])
        self.assertEqual(mandatory['profileAdjustments'],[{'airspace':'TEST','mandatoryAirspaceBand':{'min':'FLOOR','max':'CEILING'}}])
        s['restrictionType']='FORBIDDEN'
        e['verticalLimits']={'min':{'discriminator':'FL','FL':245},'max':{'discriminator':'LIMIT','LIMIT':'UNLIMITED'}}
        s['condition']['includedLocation']['elements'].append({'@class':PREFIX+'LocationConditionGroupElementTerminalArriving',
            'terminalLocation':[{'id':{'ref':{'icaoId':'LFBO'}}}]})
        self.assertEqual(self.compile(data)['rules'][0]['profileAdjustments'][0]['extensionTerminals'][0]['terminal'],'arr')

    def test_route_ptr_keeps_partial_reference_and_continuous_bounds(self):
        data=fixture();s=data['records'][0]['data']['states'][0]
        s['@class']=PREFIX+'PTRestrictionState';s['restrictionType']='FORBIDDEN'
        e={'@class':PREFIX+'RoutingElementRouteIdRelevant','routeId':{'ref':{'icaoId':'R1'}},
            'positionedObject1':{'ref':{'pointId':'A'}},'positionedObject2':{'ref':{'pointId':'C'}},
            'verticalLimits':{'min':{'discriminator':'FL','FL':245},'max':{'discriminator':'FL','FL':660}}}
        s['routing']={'sequences':[{'elements':[e]}]}
        s['condition']['includedLocation']['elements'][0]['crossingLocation']=[dict(e,**{'@class':PREFIX+'CrossingLocationElementRouteIdRelevant'})]
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        cap=rule['profileAdjustments'][0];self.assertEqual(cap['mode'],'FORBIDDEN')
        self.assertEqual(cap['minFt'],24500);self.assertTrue(cap['routeLocation']['partial'])
        self.assertTrue(cap['routeLocation']['requireFiledRoute'])

    def test_closed_for_cruising_checks_whole_route_not_only_first_segment(self):
        data=fixture();s=data['records'][0]['data']['states'][0]
        s.pop('routing');s['restrictionType']='CLOSED_FOR_CRUISING'
        s['condition']['includedLocation']['elements'][0]['crossingLocation']=[{
            '@class':PREFIX+'CrossingLocationElementRouteIdRelevant','routeId':{'ref':{'icaoId':'R1'}},
            'positionedObject1':{'ref':{'pointId':'A'}},'positionedObject2':{'ref':{'pointId':'C'}}}]
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['clauses'][0]['condition']['op'],'cruiseAt')
        self.assertEqual(rule['clauses'][0]['condition']['location']['op'],'route')
        self.assertEqual(rule['referenceLocation']['op'],'firstSegment')
        self.assertEqual(rule['conversionDiagnostics'],[])

    def test_ptr_route_conditions_use_partial_segments_and_requested_level(self):
        c=Compiler();c.state_class='PTRestrictionState';c.vertical_reference='RFL'
        e={'@class':PREFIX+'CrossingLocationElementRouteIdRelevant',
           'routeId':{'ref':{'icaoId':'R1'}},'positionedObject1':{'ref':{'pointId':'A'}},
           'positionedObject2':{'ref':{'pointId':'C'}},'verticalLimits':{
               'min':{'discriminator':'FL','FL':245},'max':{'discriminator':'LIMIT','LIMIT':'UNLIMITED'}}}
        node=c.element(e,False)
        self.assertEqual(node['op'],'segmentLevel');self.assertTrue(node['requested'])
        self.assertEqual(node['sample'],'profile');self.assertTrue(node['location']['partial'])
        self.assertTrue(node['location']['requireFiledRoute']);self.assertEqual(c.issues,[])
        e={'@class':PREFIX+'CrossingLocationElementAirspace','id':{'ref':{'airspaceId':'TEST'}}}
        self.assertEqual(c.element(e),{'op':'airspaceSlice','values':['TEST'],'min':'FLOOR','max':'CEILING','requested':True})

    def test_terminal_dct_allows_published_long_connections(self):
        data=fixture();state=data['records'][0]['data']['states'][0]
        state['@class']=PREFIX+'DctLimitRestrictionState'
        state['condition']={'@class':PREFIX+'TerminalArrivingDctLimitRestrictionCondition',
            'aerodromeId':{'ref':{'icaoId':'LFPG'}},'dctLimit':20,
            'condition':{'@class':PREFIX+'trees.GroupCondition','operator':'NOT','conditions':[
                {'@class':PREFIX+'trees.FlightTypeCondition','value':'MILITARY'}]}}
        state['routing']={'sequences':[{'elements':[{'@class':PREFIX+'RoutingElementDctSegment',
            'positionedObject1':{'ref':{'pointId':'FAR'}},'positionedObject2':{'ref':{'icaoId':'LFPG'}}}]}]}
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['clauses'][0]['condition']['allowedPoints'],['FAR'])
        self.assertEqual(rule['scope']['args'][1]['op'],'not')
        state['routing']['sequences'][0]['elements'][0]['positionedObject2']['ref']['icaoId']='LFBO'
        self.assertTrue(self.compile(data)['rules'][0]['conversionDiagnostics'])

    def test_terminal_procedure_reference_uses_first_segment(self):
        data=fixture();state=data['records'][0]['data']['states'][0]
        state['condition']['includedLocation']['elements'][0]['crossingLocation']=[{
            '@class':PREFIX+'CrossingLocationElementTerminalProcedure','id':{'ref':{'icaoId':'TEST1A'}}}]
        state['applicabilities'][0]['weeklyDayPeriodPattern']['timePattern'][0]['wef']=3600000
        rule=self.compile(data)['rules'][0]
        expected={'op':'firstSegment','location':{'op':'procedureSpan','values':['TEST1A']}}
        self.assertEqual(rule['referenceLocation'],expected)
        self.assertEqual(rule['schedule']['location'],expected)
        self.assertEqual(rule['conversionDiagnostics'],[])

    def test_supported_point_ptr_emits_bounds_and_never_a_rejection(self):
        data=fixture();state=data['records'][0]['data']['states'][0]
        state['@class']=PREFIX+'PTRestrictionState'
        element=point('A');element['verticalLimits']={
            'min':{'discriminator':'LIMIT','LIMIT':'GROUND'},'max':{'discriminator':'FL','FL':190}}
        state['routing']={'sequences':[{'elements':[element]}]}
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['profileAdjustments'],[{'point':'A','minFt':None,'maxFt':19000}])
        self.assertEqual(rule['clauses'][0]['action'],'profileAdjustment')
        # An upper-unbounded forbidden PTR carries the native 200-km extension
        # with its positive terminal condition, not just a point cap.
        state['restrictionType']='FORBIDDEN'
        element['verticalLimits']={'min':{'discriminator':'FL','FL':245},
                                  'max':{'discriminator':'LIMIT','LIMIT':'UNLIMITED'}}
        state['condition']['includedLocation']['elements'].append({
            '@class':PREFIX+'LocationConditionGroupElementTerminalArriving',
            'terminalLocation':[{'id':{'ref':{'icaoId':'LFPG'}}}]})
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['profileAdjustments'][0]['extensionTerminals'],[
            {'terminal':'arr','condition':{'op':'arr','values':['LFPG']}}])
        self.assertEqual(rule['conversionDiagnostics'],[])
        state['condition']['includedLocation']['elements'][1]['operand']='OR'
        self.assertNotIn('profileAdjustments',self.compile(data)['rules'][0])

    def test_forbidden_finite_point_ptr_keeps_both_permitted_sides(self):
        data=fixture();state=data['records'][0]['data']['states'][0]
        state['@class']=PREFIX+'PTRestrictionState';state['restrictionType']='FORBIDDEN'
        element=point('A');element['verticalLimits']={
            'min':{'discriminator':'FL','FL':245},'max':{'discriminator':'FL','FL':315}}
        state['routing']={'sequences':[{'elements':[element]}]}
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['profileAdjustments'],[{'point':'A','forbiddenBand':{'minFt':24500,'maxFt':31500}}])
        self.assertEqual(rule['conversionDiagnostics'],[])

    def test_relative_airspace_floor_and_ceiling_remain_local(self):
        compiler=Compiler();compiler.state_class='HSRestrictionState';compiler.vertical_reference='RFL'
        value={'@class':PREFIX+'CrossingLocationElementAirspace','id':{'ref':{'airspaceId':'TEST'}},
               'verticalLimits':{'min':{'discriminator':'LIMIT','LIMIT':'FLOOR'},
                                 'max':{'discriminator':'FL','FL':195}}}
        self.assertEqual(compiler.element(value,True),{'op':'airspaceSlice','values':['TEST'],
                         'min':'FLOOR','max':19500,'requested':True})
        value['verticalLimits']['min']={'discriminator':'FL','FL':195}
        value['verticalLimits']['max']={'discriminator':'LIMIT','LIMIT':'CEILING'}
        self.assertEqual(compiler.element(value)['requested'],False)
        # The runtime resolves the composed envelope of overlapping blocks.
        value['verticalLimits']['min']={'discriminator':'LIMIT','LIMIT':'CEILING'}
        value['verticalLimits']['max']={'discriminator':'LIMIT','LIMIT':'UNLIMITED'}
        self.assertEqual(compiler.element(value)['op'],'airspaceSlice')

    def test_registration_prefix_preserves_unsupported_other_masks(self):
        compiler=Compiler()
        for value in ('RA*','EW*','9H*'):
            node=compiler.property({'@class':PREFIX+'trees.AircraftRegistrationCondition','value':value})
            self.assertEqual(node,{'op':'registrationPrefix','values':[value[:-1]],'sourcePattern':value})
        for value in ('RA??','RA**','*RA','R*A','*'):
            self.assertEqual(compiler.property({'@class':PREFIX+'trees.AircraftRegistrationCondition','value':value})['op'],'unknown')

    def test_point_list_requires_complete_expanded_portion(self):
        value={'@class':PREFIX+'RoutingElementPointList',
               'routeId':{'ref':{'icaoId':'R1'}},
               'positionedObject1':{'ref':{'pointId':'AAA'}},
               'positionedObject2':{'ref':{'pointId':'CCC'}}}
        compiler=Compiler();compiler.state_class='HSRestrictionState'
        self.assertEqual(compiler.element(value),{'op':'routePointList','parts':[['AAA'],['R1'],['CCC']]})
        value['verticalLimits']={'min':{'discriminator':'FL','FL':195},
                                 'max':{'discriminator':'LIMIT','LIMIT':'UNLIMITED'}}
        node=compiler.element(value)
        self.assertEqual(node['op'],'segmentLevel')
        self.assertEqual(node['location']['op'],'routePointList')
        self.assertEqual(node['sample'],'entry')
        state=fixture()['records'][0]['data']['states'][0]
        ref=dict(value,**{'@class':PREFIX+'CrossingLocationElementPointList'})
        state['condition']['includedLocation']['elements'][0]['crossingLocation']=[ref]
        compiler.vertical_reference='CFL'
        reference=compiler.reference_location(state)
        self.assertEqual(reference['op'],'firstSegment')
        self.assertEqual(reference['location']['sample'],'endpoints')
        state['applicabilities'][0]['weeklyDayPeriodPattern']['timePattern'][0]['wef']=3600000
        self.assertEqual(compiler.applicability(state)['location'],reference)
        value['unrecognizedOption']=True
        self.assertEqual(compiler.element(value)['op'],'unknown')

    def test_terminal_dct_limit_is_maximum_distance_not_mandatory_routing(self):
        data=fixture();state=data['records'][0]['data']['states'][0]
        state.pop('routing')
        state['@class']=PREFIX+'DctLimitRestrictionState'
        state['condition']={'@class':PREFIX+'TerminalDepartingDctLimitRestrictionCondition',
            'aerodromeId':{'ref':{'icaoId':'LFPG'}},'dctLimit':20}
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['referenceLocation'],{'op':'dep','values':['LFPG']})
        self.assertEqual(rule['clauses'][0]['condition'],{'op':'terminalDctLength','terminal':'dep','value':20})

    def test_basic_calendar_preserves_concrete_end_and_negative_applicability(self):
        data=fixture();state=data['records'][0]['data']['states'][0]
        state['applicabilities']=[]
        period={'@class':'WeeklyDayPeriod','weekDays':DAYS,'wef':0,'til':0}
        state['eventApplicabilities']=[{'timeFrame':{'discriminator':'ExtendedDatesPair','ExtendedDatesPair':{
            'wef':{'discriminator':'Concrete','Concrete':START-86400000},
            'til':{'discriminator':'Concrete','Concrete':START}}},
            'specialDayPeriod':{'timePattern':{'period':period},'excludeTimePattern':False,
                'wefExtTimeInDay':{'timeReference':'MIDNIGHT','offset':0},
                'tilExtTimeInDay':{'timeReference':'MIDNIGHT','offset':86400000}}}]
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['schedule']['schedule']['args'][0],{'op':'utcInterval','start':START-86400000,'end':START})
        state['autoDSTChangeEnabled']=True
        period.update(wef=18000000,til=75600000)
        special=state['eventApplicabilities'][0]['specialDayPeriod']
        special['wefExtTimeInDay']['offset']=18000000
        special['tilExtTimeInDay']['offset']=75600000
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        self.assertEqual(rule['schedule']['schedule']['args'][1]['schedule'],{'op':'time','start':300,'end':1260})
        state['duringApplicability']=False
        negative=self.compile(data)['rules'][0]['schedule']['schedule']
        self.assertEqual(negative['op'],'not')
        self.assertEqual(negative['args'][0],rule['schedule']['schedule'])
        state['eventApplicabilities'][0]['specialDayPeriod']['excludeTimePattern']=True
        self.assertEqual(self.compile(data)['rules'][0]['schedule']['op'],'unknown')

    def test_fua_basic_calendar_does_not_reuse_expired_allocation_expansion(self):
        data=fixture();state=data['records'][0]['data']['states'][0]
        state['eventApplicabilities']=[{'timeFrame':{'discriminator':'ExtendedDatesPair','ExtendedDatesPair':{
            'wef':{'discriminator':'Concrete','Concrete':START},'til':{'discriminator':'Concrete','Concrete':END}}},
            'specialDayPeriod':{'timePattern':{'period':{'@class':'WeeklyDayPeriod','weekDays':DAYS,'wef':0,'til':0}},
                'excludeTimePattern':False,'wefExtTimeInDay':{'timeReference':'MIDNIGHT','offset':0},
                'tilExtTimeInDay':{'timeReference':'MIDNIGHT','offset':86400000}}}]
        state['applicabilities']=[]
        state['dependentApplicability']={'@class':'DependentApplicability','fua':True,'fuaDefaultActive':True,
            'fuaRad':False,'dependentVerticalLimits':True,'extendedVerticalLimits':False,
            'condition':{'@class':'DependentApplicabilityAirspaceActivation','airspace':{'ref':{'backendId':'A','airspaceId':'A'}},
                         'offsetWef':-15,'offsetTil':20}}
        rule=self.compile(data)['rules'][0]
        self.assertEqual(rule['conversionDiagnostics'],[])
        activation=rule['schedule']['schedule']['args'][1]
        self.assertEqual(activation['op'],'nativeActivation')
        self.assertEqual((activation['offsetStartMinutes'],activation['offsetEndMinutes']),(-15,20))
        self.assertTrue(activation['fua']);self.assertTrue(activation['vertical'])

    def test_dct_vertical_limits_keep_entry_and_reference_sampling_distinct(self):
        c=Compiler();c.state_class='HSRestrictionState';c.vertical_reference='CFL'
        value={'@class':PREFIX+'RoutingElementDctSegment',
               'positionedObject1':{'ref':{'pointId':'AAA'}},'positionedObject2':{'ref':{'pointId':'BBB'}},
               'verticalLimits':{'min':{'discriminator':'FL','FL':195},'max':{'discriminator':'FL','FL':285}}}
        node=c.element(value)
        self.assertEqual(node['op'],'segmentLevel');self.assertEqual(node['sample'],'entry')
        self.assertEqual(len(node['checks']),2)
        self.assertEqual(c.element(value,True)['sample'],'endpoints')
        c.vertical_reference='RFL'
        self.assertEqual(c.element(value,True)['sample'],'profile')
        self.assertTrue(c.element(value,True)['requested'])

    def test_point_levels_use_rfl_only_at_reference_and_keep_both_bounds(self):
        compiler = Compiler()
        compiler.state_class = 'HSRestrictionState'
        compiler.vertical_reference = 'RFL'
        value = point('A')
        value['verticalLimits'] = {'min':{'discriminator':'FL','FL':195},'max':{'discriminator':'FL','FL':285}}
        node = compiler.element(value, reference=True)
        self.assertEqual((node['comparison'],node['value'],node['requested']),('BLW',28500,True))
        self.assertEqual((node['location']['comparison'],node['location']['value']),('ABV',19500))
        self.assertFalse(compiler.element(value)['requested'])
        self.assertEqual(node['location']['location'],{'op':'via','values':['A']})
        value['@class'] = PREFIX+'RoutingElementAirspace'
        self.assertEqual(compiler.element(value)['location']['location']['op'],'airspaceFootprint')

    def test_incomplete_or_unverified_catalogue_cannot_be_published(self):
        result = self.compile(fixture())
        self.assertIn('runtimeValidationPending', publication_blockers(result))
        result['operationallyValidated'] = True
        result['globalPendingValidation'] = []
        self.assertEqual(publication_blockers(result), [])
        result['summary']['missingRestrictions'] = 1
        self.assertIn('missingRestrictions', publication_blockers(result))
        report = integration_report(result)
        self.assertEqual(report['rulesWithoutPendingSemantics'], 1)
        self.assertEqual(report['sourceFiles'], result['sourceFiles'])

    def test_native_aircraft_classification_and_operator_are_preserved(self):
        compiler = Compiler()
        self.assertEqual(compiler.property({'@class': PREFIX+'AircraftTypeClassificationCondition', 'value':'?4?'}),
                         {'op':'aircraftClassification','values':['?4?']})
        self.assertEqual(compiler.property({'@class': PREFIX+'AircraftTypeClassificationCondition', 'value':'?5?'})['op'], 'unknown')
        ref={'@class':'UnitRef','unitId':'BRU','backendId':'BRU:1','uuid':'test'}
        node=compiler.property({'@class':PREFIX+'AircraftOperatorIdCondition','value':ref})
        self.assertEqual(node,{'op':'aircraftOperator','values':['BRU'],'sourceOperatorRef':ref})

    def test_publication_checks_expressions_even_when_summary_claims_complete(self):
        result = self.compile(fixture())
        result.update(operationallyValidated=True, globalPendingValidation=[])
        result['rules'][0]['clauses'][0]['condition'] = {'op':'not', 'args':[{'op':'unknown'}]}
        self.assertIn('pendingSemantics', publication_blockers(result))

    def test_subday_validity_preserves_exact_reference_interval(self):
        data = fixture()
        period = data['records'][0]['data']['states'][0]['period']
        period.update(start=START+3600000, end=END-1800000)
        rule = self.compile(data)['rules'][0]
        self.assertEqual(rule['sourceValidity'], period)
        self.assertEqual(rule['conversionDiagnostics'], [])

    def test_terminal_airspace_uses_ground_projection(self):
        ref = {'airspaceId':'TESTCTA', 'backendId':'TESTCTA:1'}
        value = {'@class':'AirspaceSummary', 'ref':ref}
        for terminal in ('dep', 'arr'):
            self.assertEqual(Compiler().entity(terminal, value), {
                'op':'airportInAirspace','terminal':terminal,'values':['TESTCTA'],'sourceAirspaceRef':ref})

    def test_entity_identity_cannot_differ_from_index(self):
        data = fixture()
        data['records'][0]['data']['ref'] = dict(data['records'][0]['summary']['ref'], restrictionId='WRONG')
        with self.assertRaisesRegex(ValueError, 'identity differs'):
            self.compile(data)

    def compile(self, data):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'capture.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            return compile_captures([path])

    def test_mandatory_keeps_order_and_negates_routing(self):
        result = self.compile(fixture())
        rule = result['rules'][0]
        self.assertEqual(rule['clauses'][0]['condition'], {
            'op': 'not', 'args': [{'op': 'ordered', 'args': [
                {'op': 'via', 'values': ['B']}, {'op': 'via', 'values': ['C']}]}]})
        self.assertEqual(rule['schedule'], {'op': 'true'})
        self.assertEqual(rule['conversionDiagnostics'], [])
        self.assertFalse(result['operationallyValidated'])

    def test_source_state_is_lossless(self):
        data = fixture()
        state = data['records'][0]['data']['states'][0]
        state['newSemanticFlag'] = {'any': ['new', 'values']}
        before = copy.deepcopy(data)
        result = self.compile(data)
        self.assertEqual(data, before)
        self.assertEqual(result['rules'][0]['sourceState'], state)
        self.assertIn('unhandledStateFields', result['summary']['pendingSemantics'])

    def test_end_is_exclusive_and_new_revision_survives(self):
        data = fixture()
        state = data['records'][0]['data']['states'][0]
        old = copy.deepcopy(state)
        old.update(stateId='old', period={'start': START - 86400000, 'end': START})
        data['records'][0]['data']['states'].insert(0, old)
        result = self.compile(data)
        self.assertEqual(len(result['rules']), 1)
        self.assertEqual(result['rules'][0]['validUntil'], '2026-11-25')

    def test_disabled_state_is_tombstone(self):
        data = fixture()
        data['records'][0]['data']['states'][0]['enabled'] = 'Enabled_FALSE'
        self.assertEqual(self.compile(data)['rules'][0]['change'], 'SUS')

    def test_unknown_location_is_never_true(self):
        compiler = Compiler()
        node = compiler.element({'@class': PREFIX + 'RoutingElementNewKind'})
        self.assertEqual(node['op'], 'unknown')
        self.assertTrue(compiler.issues)

    def test_empty_routing_is_indeterminate(self):
        self.assertEqual(Compiler().routing({'sequences': []})['op'], 'unknown')

    def test_extended_slice_cannot_collapse_to_false_via_base_volume(self):
        value = point('AREA')
        value['@class'] = PREFIX + 'RoutingElementAirspace'
        value['verticalLimits'] = {'min': {'LIMIT': 'CEILING'}, 'max': {'LIMIT': 'UNLIMITED'}}
        self.assertEqual(Compiler().element(value)['op'], 'unknown')

    def test_equipment_is_mapped_only_for_icao_tokens(self):
        compiler = Compiler()
        self.assertEqual(compiler.property({'@class': PREFIX + 'AEComNavCondition', 'value': 'J5'}),
                         {'op': 'comNavEquipment', 'values': ['J5']})
        self.assertEqual(compiler.property({'@class': PREFIX + 'AEComNavCondition', 'value': 'VHF'})['op'], 'unknown')

    def test_closed_reference_does_not_require_routing(self):
        data = fixture()
        state = data['records'][0]['data']['states'][0]
        state.pop('routing')
        state['restrictionType'] = 'CLOSED'
        self.assertEqual(self.compile(data)['rules'][0]['clauses'][0]['condition'], {'op': 'true'})

    def test_calendar_horizon_cannot_be_extrapolated(self):
        data = fixture()
        state = data['records'][0]['data']['states'][0]
        state['applicabilities'][0]['weeklyDayPeriodPattern']['timeFrame']['end'] = START + 86400000
        self.assertEqual(self.compile(data)['rules'][0]['schedule']['op'], 'unknown')

    def test_aerodrome_set_uses_internal_identity_and_validity(self):
        ref = {'backendId': 'GROUP:2', 'aerodromeSetId': 'GROUP'}
        summary = {'@class': 'AerodromeSetSummary', 'ref': ref}
        dep = {'ref': ref, 'states': [{'period': {'start': START, 'end': END},
                                    'implicitAerodromes': ['BI'],
                                    'explicitAerodromes': [{'ref': {'icaoId': 'EGLL'}}]}]}
        compiler = Compiler({'az:GROUP:2': dep})
        self.assertEqual(compiler.entity('dep', summary)['values'], ['BI**', 'EGLL'])
        dep['states'][0]['period']['end'] = START + 86400000
        self.assertEqual(compiler.entity('dep', summary)['op'], 'unknown')
        self.assertEqual(Compiler().entity('dep', summary)['op'], 'unknown')

    def test_calendar_uses_reference_location_not_eobt(self):
        data = fixture()
        state = data['records'][0]['data']['states'][0]
        period = state['applicabilities'][0]['weeklyDayPeriodPattern']['timePattern'][0]
        period.update(weekDays=['MONDAY'], wef=22 * 3600000, til=2 * 3600000)
        node = self.compile(data)['rules'][0]['schedule']
        self.assertEqual(node['op'], 'timeAt')
        self.assertEqual(node['location'], {'op': 'via', 'values': ['A']})
        week = node['schedule']['args'][1]
        self.assertEqual(week['days'], [0])
        self.assertEqual(week['schedule'], {'op': 'time', 'start': 1320, 'end': 120})

    def test_procedure_matches_its_filed_designator(self):
        value = {'@class': PREFIX + 'RoutingElementTerminalProcedure',
                 'id': {'ref': {'icaoId': 'TEST1A', 'backendId': 'TEST1A'}}}
        self.assertEqual(Compiler().element(value), {'op': 'route', 'parts': [['TEST1A']]})

    def test_profile_tuning_is_not_an_fpl_rejection(self):
        data = fixture()
        data['records'][0]['data']['states'][0]['@class'] = PREFIX + 'PTRestrictionState'
        rule = self.compile(data)['rules'][0]
        self.assertEqual(rule['effect'], 'profileAdjustment')
        self.assertEqual(rule['clauses'][0]['condition']['op'], 'unknown')
        self.assertEqual(rule['clauses'][0]['condition']['reason'], 'database:profileAdjustment')

    def test_aerodrome_ifr_restriction_keeps_terminal_phase(self):
        data = fixture()
        state = data['records'][0]['data']['states'][0]
        state.update({'@class': PREFIX + 'AerodromeFlightRuleRestrictionState',
                      'restrictionType': 'FORBIDDEN',
                      'condition': {'@class': PREFIX + 'AerodromeFlightRuleRestrictionCondition',
                                    'terminalType': 'ARRIVAL', 'aerodrome': {'ref': {'icaoId': 'BITE'}}}})
        state.pop('routing')
        rule = self.compile(data)['rules'][0]
        self.assertEqual(rule['scope'], {'op': 'arr', 'values': ['BITE']})
        self.assertIn({'op': 'flightRulesAtTerminal', 'terminal': 'arr'}, rule['clauses'][0]['condition']['args'])
        state['condition']['terminalType']='DEPARTURE_ARRIVAL'
        rule=self.compile(data)['rules'][0]
        branches=rule['clauses'][0]['condition']['args']
        self.assertEqual(len(branches),2)
        for branch,phase in zip(branches,('dep','arr')):
            self.assertEqual(branch['args'][1]['terminal'],phase)
            self.assertEqual(branch['args'][2]['location']['op'],phase)
            self.assertEqual(branch['args'][2]['schedule']['args'][1],
                {'op':'utcInterval','start':START,'end':END})
        self.assertEqual(rule['conversionDiagnostics'],[])

    def test_incomplete_capture_is_counted(self):
        data = fixture()
        data['index'].append({'RESTRICTION': {'ref': {'backendId': 'MISSING'}}})
        result = self.compile(data)
        self.assertEqual(result['missingBackendIds'], ['MISSING'])
        self.assertEqual(result['summary']['capturedRestrictions'], 1)

    def test_terminal_procedure_restriction_keeps_airport_property_and_action(self):
        data = fixture()
        state = data['records'][0]['data']['states'][0]
        state.update({'@class': PREFIX + 'TpAircraftTypeRestrictionState',
            'condition': {'@class': PREFIX + 'TpAircraftTypeRestrictionCondition',
                'terminalType': 'DEPARTURE', 'aerodrome': {'ref': {'icaoId': 'EBBR'}},
                'condition': {'@class': PREFIX + 'AircraftTypeCondition',
                              'value': {'icaoId': 'A320'}}},
            'restrictionType': 'FORBIDDEN',
            'routing': {'sequences': [{'elements': [{
                '@class': PREFIX + 'RoutingElementTerminalProcedure',
                'id': {'ref': {'icaoId': 'LNO2P'}}}]}]}})
        rule = self.compile(data)['rules'][0]
        self.assertEqual(rule['scope'], {'op': 'and', 'args': [
            {'op': 'dep', 'values': ['EBBR']}, {'op': 'aircraft', 'values': ['A320']}]})
        self.assertEqual(rule['clauses'][0]['condition'], {'op': 'route', 'parts': [['LNO2P']]})
        state['restrictionType'] = 'MANDATORY'
        self.assertEqual(self.compile(data)['rules'][0]['clauses'][0]['condition']['op'], 'not')
        period = state['applicabilities'][0]['weeklyDayPeriodPattern']['timePattern'][0]
        period.update(wef=25200000, til=61200000)
        schedule = self.compile(data)['rules'][0]['schedule']
        self.assertEqual(schedule['op'], 'timeAt')
        self.assertEqual(schedule['location'], {'op': 'dep', 'values': ['EBBR']})
        state['condition']['terminalType'] = 'DEPARTURE_ARRIVAL'
        self.assertEqual(self.compile(data)['rules'][0]['schedule']['op'], 'unknown')

    def test_nested_semantic_extensions_are_not_silently_ignored(self):
        mutations = [
            ('cell', lambda s: s['condition']['includedLocation']['elements'][0], 'unhandledCellFields'),
            ('group', lambda s: s['condition']['includedLocation'], 'unhandledLocationGroupFields'),
            ('routing', lambda s: s['routing'], 'unhandledRoutingFields'),
            ('sequence', lambda s: s['routing']['sequences'][0], 'unhandledRoutingSequenceFields'),
        ]
        for label, select, reason in mutations:
            with self.subTest(label=label):
                data = fixture()
                select(data['records'][0]['data']['states'][0])['newSemanticFlag'] = True
                result = self.compile(data)
                self.assertIn(reason, result['summary']['pendingSemantics'])
        node = Compiler().property({'@class': PREFIX + 'FlightTypeCondition',
                                    'value': 'SCHEDULED', 'negated': True})
        self.assertEqual(node['op'], 'unknown')
        self.assertEqual(node['reason'], 'database:unhandledPropertyFields')
        modified_point = dict(point('A'), exclusion=True)
        self.assertEqual(Compiler().element(modified_point)['op'], 'unknown')
        flow = fixture()['records'][0]['data']['states'][0]['condition']
        flow['exclusion'] = True
        self.assertEqual(Compiler().flow(flow)['op'], 'unknown')

    def test_terminal_discriminator_and_extra_semantics(self):
        cell = {'@class': PREFIX + 'LocationConditionGroupElementTerminalDeparting',
                'terminalLocation': [{'@class': PREFIX + 'TerminalLocationElement',
                    'type': 'AERODROME', 'id': {'discriminator': 'AERODROME',
                        'AERODROME': {'ref': {'icaoId': 'LFPG'}}}}]}
        self.assertEqual(Compiler().cell(cell), {'op': 'dep', 'values': ['LFPG']})
        cell['terminalLocation'][0]['type'] = 'AIRSPACE'
        self.assertEqual(Compiler().cell(cell)['reason'], 'database:terminalTypeMismatch')
        cell['terminalLocation'][0]['type'] = 'AERODROME'
        cell['terminalLocation'][0]['newSemanticFlag'] = True
        self.assertEqual(Compiler().cell(cell)['reason'], 'database:unhandledTerminalFields')

    def test_full_vertical_range_does_not_limit_a_point_but_can_extend_airspace(self):
        value = point('A')
        value['verticalLimits'] = {
            'min': {'discriminator': 'LIMIT', 'LIMIT': 'GROUND'},
            'max': {'discriminator': 'LIMIT', 'LIMIT': 'UNLIMITED'}}
        self.assertEqual(Compiler().element(value), {'op': 'via', 'values': ['A']})
        value['@class'] = PREFIX + 'RoutingElementAirspace'
        self.assertEqual(Compiler().element(value)['op'], 'unknown')
        value['@class'] = PREFIX + 'RoutingElementPoint'
        value['verticalLimits']['min'] = {'discriminator': 'FL', 'FL': 195}
        self.assertEqual(Compiler().element(value)['op'], 'unknown')

    def test_negative_basic_applicability_uses_effective_expansion_once(self):
        data = fixture()
        state = data['records'][0]['data']['states'][0]
        state['duringApplicability'] = False
        # Native negative basic periods 00-07 and 17-24 expand to effective 07-17.
        state['eventApplicabilities'] = [
            {'specialDayPeriod': {'timePattern': {'period': {'wef': 0, 'til': 25200000}}}},
            {'specialDayPeriod': {'timePattern': {'period': {'wef': 61200000, 'til': 0}}}},
        ]
        period = state['applicabilities'][0]['weeklyDayPeriodPattern']['timePattern'][0]
        period.update(wef=25200000, til=61200000)
        schedule = self.compile(data)['rules'][0]['schedule']
        self.assertEqual(schedule['op'], 'timeAt')
        self.assertEqual(schedule['schedule']['args'][1]['schedule'],
                         {'op': 'time', 'start': 420, 'end': 1020})
    def test_mixed_cycle_is_rejected(self):
        data = fixture()
        data['referenceStart'] += 86400000
        with self.assertRaises(ValueError):
            self.compile(data)

    def test_conflicting_duplicate_is_rejected(self):
        data = fixture()
        other = copy.deepcopy(data['records'][0])
        other['data']['states'][0]['restrictionType'] = 'FORBIDDEN'
        data['records'].append(other)
        with self.assertRaises(ValueError):
            self.compile(data)

    def test_audit_counts_unique_entities_and_all_states(self):
        data = fixture()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'capture.json'
            path.write_text(json.dumps(data), encoding='utf-8')
            result = audit([path, path])
        self.assertEqual(result['capturedRestrictions'], 1)
        self.assertEqual(result['capturedStates'], 1)
        self.assertTrue(result['captureComplete'])
        self.assertFalse(result['conversionValidated'])


if __name__ == '__main__':
    unittest.main()
