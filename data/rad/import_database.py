"""Compile captured NMP restrictions to an auditable iATFM candidate.

Never publishes catalogue.json. The source state is retained without changes;
unimplemented semantics are explicit unknown nodes, never an empty condition.
Compilation coverage is not operational validation or extraction coverage.
"""
import collections
import copy
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

TRUE = {'op': 'true'}
DAYS = ['MONDAY', 'TUESDAY', 'WEDNESDAY', 'THURSDAY', 'FRIDAY', 'SATURDAY', 'SUNDAY']
START = 1793232000000
END = START + 28 * 86400000


def kind(value):
    return value.get('@class', '').rsplit('.', 1)[-1]


def combine(op, nodes):
    nodes = list(nodes)
    if not nodes:
        return {'op': 'true' if op == 'and' else 'false'}
    return nodes[0] if len(nodes) == 1 else {'op': op, 'args': nodes}


def negate(node):
    return {'op': 'not', 'args': [node]}


def identifier(value):
    if not isinstance(value, dict):
        return None
    if 'discriminator' in value and value['discriminator'] in value:
        return identifier(value[value['discriminator']])
    ref = value.get('ref', value)
    for key in ('pointId', 'icaoId', 'airspaceId', 'aerodromeSetId', 'restrictionId'):
        if key in ref:
            return ref[key]
    return None


def iso_day(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime('%Y-%m-%d')


class Compiler:
    def __init__(self, dependencies=None):
        self.issues = []
        self.dependencies = dependencies or {}
        self.window = (START, END)
        self.source_paths = {}
        self.state_class = None
        self.vertical_reference = None

    def unknown(self, reason, value):
        cls = kind(value) if isinstance(value, dict) else ''
        pointer = self.source_paths.get(id(value), '')
        self.issues.append({'reason': reason, 'sourceClass': cls, 'sourcePointer': pointer})
        # The complete original state lives once in sourceState. Repeating it
        # in each unsupported expression multiplies a Europe-wide export's RAM
        # and disk requirements, and obscures which source field needs work.
        return {'op': 'unknown', 'reason': 'database:' + reason,
                'sourceClass': cls, 'sourcePointer': pointer}

    def entity(self, op, value):
        name = identifier(value)
        if name is None:
            return self.unknown('missingEntityIdentifier', value)
        # Sets must be resolved by backendId, never guessed from a wildcard or
        # an identically named Excel annex group.
        wrapped = value.get(value.get('discriminator'), value)
        if kind(wrapped) == 'AerodromeSetSummary':
            ref = wrapped['ref']
            entity = self.dependencies.get('az:' + ref['backendId'])
            if entity is None or entity.get('ref') != ref:
                return self.unknown('unresolvedAerodromeSet', value)
            start, end = self.window
            states = [s for s in entity['states'] if s['period']['start'] <= start and s['period']['end'] >= end]
            if len(states) != 1:
                return self.unknown('aerodromeSetValidity', value)
            state = states[0]
            allowed = {'@class', 'period', 'stateId', 'name', 'displayName', 'note',
                       'capacities', 'explicitAerodromes', 'implicitAerodromes'}
            if set(state) - allowed:
                return self.unknown('aerodromeSetFields', state)
            members = [identifier(x) for x in state.get('explicitAerodromes', [])]
            for prefix in state.get('implicitAerodromes', []):
                if not isinstance(prefix, str) or not 1 <= len(prefix) <= 4 or not prefix.isascii() or not prefix.isalpha() or not prefix.isupper():
                    return self.unknown('implicitAerodromePattern', state)
                members.append(prefix.ljust(4, '*'))
            if any(not isinstance(x, str) or len(x) != 4 for x in members):
                return self.unknown('explicitAerodromeIdentifier', state)
            return {'op': op, 'values': sorted(set(members)), 'sourceAerodromeSet': ref}
        if op in ('dep', 'arr') and kind(wrapped) == 'AirspaceSummary':
            # NM terminal airspace conditions use the ground projection,
            # irrespective of the original blocks' floor/ceiling.
            return {'op': 'airportInAirspace', 'terminal': op, 'values': [name],
                    'sourceAirspaceRef': wrapped.get('ref', {})}
        return {'op': op, 'values': [name]}

    def element(self, value, reference=False):
        cls = kind(value)
        suffix = cls.removeprefix('CrossingLocationElement').removeprefix('RoutingElement')
        if self.state_class=='PTRestrictionState' and suffix not in ('Point','Airspace','RouteIdRelevant','PointList','DctSegment'):
            return self.unknown('profileLocation',value)
        if suffix in ('Point', 'Airspace'):
            node = self.entity('via', value.get('id', {}))
        elif suffix == 'TerminalProcedure':
            name = identifier(value.get('id', {}))
            if not name:
                return self.unknown('missingProcedureIdentifier', value)
            node = {'op': 'procedureSpan', 'values':[name]} if reference else {'op': 'route', 'parts': [[name]]}
        elif suffix in ('DctSegment', 'RouteIdRelevant', 'PointList'):
            a, b = identifier(value.get('positionedObject1', {})), identifier(value.get('positionedObject2', {}))
            route = 'DCT' if suffix == 'DctSegment' else identifier(value.get('routeId', {}))
            if not all((a, b, route)):
                return self.unknown('missingRouteEndpoint', value)
            node = {'op': 'route', 'parts': [[a], [route], [b]]}
            if suffix == 'PointList':
                node['op'] = 'routePointList'
            if self.state_class=='PTRestrictionState' and suffix in ('RouteIdRelevant','PointList'):
                node.update(op='routePointList',partial=True,requireFiledRoute=suffix=='RouteIdRelevant')
        elif suffix == 'AirspaceBorder':
            node = {'op': 'boundary', 'from': self.entity('via', value.get('airspace1', {})),
                    'to': self.entity('via', value.get('airspace2', {})), 'bidirectional': False}
        else:
            return self.unknown('location:' + cls, value)
        if 'fraType' in value:
            node = combine('and', [node, self.unknown('fraPointRole', value)])
        limits = value.get('verticalLimits', {})
        if self.state_class=='PTRestrictionState' and suffix=='Airspace' and not limits:
            limits={'min':{'discriminator':'LIMIT','LIMIT':'FLOOR'},
                    'max':{'discriminator':'LIMIT','LIMIT':'CEILING'}}
        full_range = (suffix in ('Point', 'DctSegment', 'RouteIdRelevant', 'PointList', 'TerminalProcedure')
                      and set(limits) == {'min', 'max'}
                      and all(set(limits[k]) <= {'@class', 'discriminator', 'LIMIT'} for k in ('min', 'max'))
                      and limits.get('min', {}).get('discriminator') == 'LIMIT'
                      and limits.get('max', {}).get('discriminator') == 'LIMIT'
                      and limits.get('min', {}).get('LIMIT') == 'GROUND'
                      and limits.get('max', {}).get('LIMIT') == 'UNLIMITED')
        if limits and not full_range:
            # Existing level operators do not yet implement all source slice,
            # requested/current-level and per-segment semantics.
            # An extended slice can include positions outside the source
            # airspace's normal volume. Keeping a plain via AND unknown would
            # incorrectly collapse that case to false.
            point_level = self.point_vertical(node, limits, reference) if suffix == 'Point' and 'fraType' not in value else None
            if suffix in ('DctSegment','RouteIdRelevant','PointList'):
                point_level = self.segment_vertical(node, limits, reference)
            if suffix == 'Airspace':
                footprint = self.entity('airspaceFootprint', value.get('id', {}))
                point_level = self.point_vertical(footprint, limits, reference)
                if point_level is None:
                    point_level = self.relative_airspace_slice(value, limits, reference)
            node = point_level if point_level is not None else self.unknown('localVerticalLimits', value)
        extras = set(value) - {'@class', 'id', 'positionedObject1', 'positionedObject2',
                               'routeId', 'airspace1', 'airspace2', 'verticalLimits', 'fraType'}
        if extras:
            node = self.unknown('unhandledLocationFields', value)
        return node

    def relative_airspace_slice(self, value, limits, reference):
        if (self.state_class not in ('HSRestrictionState','PTRestrictionState') or set(limits) != {'min','max'}
                or (reference and self.vertical_reference not in ('CFL','RFL'))):
            return None
        parsed={}
        for key,allowed in (('min',('GROUND','FLOOR','CEILING')),('max',('UNLIMITED','CEILING','FLOOR'))):
            bound=limits[key]
            if not isinstance(bound,dict):return None
            if (bound.get('discriminator')=='LIMIT' and bound.get('LIMIT') in allowed
                    and set(bound)<={'@class','discriminator','LIMIT'}):
                parsed[key]=bound['LIMIT']
            elif (bound.get('discriminator')=='FL' and type(bound.get('FL')) is int
                  and 0<=bound['FL']<=999 and set(bound)<={'@class','discriminator','FL'}):
                parsed[key]=bound['FL']*100
            else:return None
        if not any(v in ('FLOOR','CEILING') for v in parsed.values()):return None
        name=identifier(value.get('id',{}))
        if not name:return None
        return {'op':'airspaceSlice','values':[name],**parsed,
                'requested':bool(self.state_class=='PTRestrictionState' or (reference and self.vertical_reference=='RFL'))}

    def segment_vertical(self, node, limits, reference):
        levels = self.point_vertical(TRUE, limits, reference)
        if levels is None:
            return None
        checks = []
        while levels.get('op') == 'level':
            checks.append({k: levels[k] for k in ('comparison', 'value')})
            levels = levels['location']
        return {'op':'segmentLevel','location':node,'checks':checks,
                'sample':'profile' if self.state_class=='PTRestrictionState' or (reference and self.vertical_reference == 'RFL') else ('endpoints' if reference else 'entry'),
                'requested':bool(self.state_class=='PTRestrictionState' or (reference and self.vertical_reference == 'RFL'))}

    def point_vertical(self, node, limits, reference):
        # NM Flight Planning Requirements 6.0, 3.3.4.3: ABV/BLW/BTN exclude
        # their numerical bounds. RFL applies only at the reference location;
        # the remaining conditions and flow routings use the calculated profile.
        if self.state_class not in ('HSRestrictionState','PTRestrictionState') or set(limits) != {'min', 'max'}:
            return None
        if reference and self.vertical_reference not in ('CFL', 'RFL'):
            return None
        parsed = []
        for key, unbounded in (('min', 'GROUND'), ('max', 'UNLIMITED')):
            bound = limits[key]
            if not isinstance(bound, dict):
                return None
            if bound.get('discriminator') == 'LIMIT' and bound.get('LIMIT') == unbounded and set(bound) <= {'@class','discriminator','LIMIT'}:
                parsed.append(None)
            elif (bound.get('discriminator') == 'FL' and type(bound.get('FL')) is int
                  and 0 <= bound['FL'] <= 999 and set(bound) <= {'@class','discriminator','FL'}):
                parsed.append(bound['FL'] * 100)
            else:
                return None
        if all(v is not None for v in parsed) and parsed[0] >= parsed[1]:
            return None
        for comparison, amount in zip(('ABV','BLW'), parsed):
            if amount is not None:
                node = {'op':'level','comparison':comparison,'value':amount,'location':node,
                        'requested':bool(self.state_class=='PTRestrictionState' or (reference and self.vertical_reference == 'RFL'))}
        return node

    def point_profile_adjustment(self,state):
        sequences=state.get('routing',{}).get('sequences',[])
        if set(state.get('routing',{}))-{'sequences'} or len(sequences)!=1:return None
        sequence=sequences[0]
        if set(sequence)-{'elements'} or len(sequence.get('elements',[]))!=1:return None
        element=sequence['elements'][0]
        if kind(element)!='RoutingElementPoint' or set(element)-{'@class','id','verticalLimits'}:return None
        name=identifier(element.get('id',{}));limits=element.get('verticalLimits',{})
        if not name or set(limits)!={'min','max'}:return None
        references=[e for key in ('includedLocation','secondLocation')
            for cell in state.get('condition',{}).get(key,{}).get('elements',[]) if cell.get('referenceLocation') is True
            for e in cell.get('crossingLocation',[])]
        if not references or any(kind(e)!='CrossingLocationElementPoint' or identifier(e.get('id',{}))!=name for e in references):return None
        values=[]
        for key,unbounded in (('min','GROUND'),('max','UNLIMITED')):
            b=limits[key]
            if (b.get('discriminator')=='LIMIT' and b.get('LIMIT')==unbounded
                    and set(b)<={'@class','discriminator','LIMIT'}):values.append(None)
            elif (b.get('discriminator')=='FL' and type(b.get('FL')) is int
                    and 0<=b['FL']<=999 and set(b)<={'@class','discriminator','FL'}):values.append(b['FL']*100)
            else:return None
        low,high=values;action=state.get('restrictionType');extensions=[]
        if action=='FORBIDDEN':
            if low is not None and high is None:
                extensions=self.ptr_terminal_extensions(state.get('condition',{}))
                if extensions is None:return None
                low,high=None,low
            elif low is None and high is not None:low,high=high,None
            elif low is not None and high is not None and low<high:
                # Keep the disjunction. The profiler chooses a permitted side
                # using the raw profile and RFL, then verifies the rebuilt bound.
                return {'point':name,'forbiddenBand':{'minFt':low,'maxFt':high}}
            else:return None
        elif action!='MANDATORY':return None
        if (low is None and high is None) or (low is not None and high is not None and low>high):return None
        result={'point':name,'minFt':low,'maxFt':high}
        if extensions:result['extensionTerminals']=extensions
        return result

    def ptr_terminal_extensions(self,condition):
        # Correlate each positive terminal with its condition. A disjunction
        # between condition groups needs branch-specific extension semantics.
        if condition.get('logicalOperandLocation','AND') not in ('AND','AND_NOT'):return None
        result=[]
        for key in ('includedLocation','secondLocation'):
            if key=='secondLocation' and condition.get('logicalOperandLocation')=='AND_NOT':continue
            cells=condition.get(key,{}).get('elements',[])
            # A disjunction containing only crossing conditions does not
            # alter the positive terminal already required by the other group.
            if not any(kind(c) in ('LocationConditionGroupElementTerminalDeparting','LocationConditionGroupElementTerminalArriving') for c in cells):continue
            if any(c.get('operand','AND') not in ('AND','AND_THEN') for c in cells[1:]):return None
            for cell in cells:
                terminal={'LocationConditionGroupElementTerminalDeparting':'dep',
                          'LocationConditionGroupElementTerminalArriving':'arr'}.get(kind(cell))
                if terminal:result.append({'terminal':terminal,'condition':self.cell(cell)})
        return result

    def airspace_profile_adjustment(self,state):
        action=state.get('restrictionType')
        if action not in ('FORBIDDEN','MANDATORY'):return None
        routing=state.get('routing',{});seq=routing.get('sequences',[])
        if set(routing)-{'sequences'} or len(seq)!=1 or set(seq[0])-{'elements'} or len(seq[0].get('elements',[]))!=1:return None
        element=seq[0]['elements'][0]
        if kind(element)!='RoutingElementAirspace' or set(element)-{'@class','id','verticalLimits'}:return None
        name=identifier(element.get('id',{}))
        refs=[e for key in ('includedLocation','secondLocation')
              for cell in state.get('condition',{}).get(key,{}).get('elements',[]) if cell.get('referenceLocation') is True
              for e in cell.get('crossingLocation',[])]
        if not name or not refs or any(kind(e)!='CrossingLocationElementAirspace' or identifier(e.get('id',{}))!=name for e in refs):return None
        limits=element.get('verticalLimits',{'min':{'discriminator':'LIMIT','LIMIT':'FLOOR'},
            'max':{'discriminator':'LIMIT','LIMIT':'CEILING'}})
        if set(limits)!={'min','max'}:return None
        parsed={}
        for k,allowed in (('min',('GROUND','FLOOR')),('max',('CEILING','UNLIMITED'))):
            b=limits[k]
            if set(b)<={'@class','discriminator','LIMIT'} and b.get('discriminator')=='LIMIT' and b.get('LIMIT') in allowed:
                parsed[k]=b['LIMIT']
            elif set(b)<={'@class','discriminator','FL'} and b.get('discriminator')=='FL' and type(b.get('FL')) is int and 0<=b['FL']<=999:
                parsed[k]=b['FL']*100
            else:return None
        if all(isinstance(v,int) for v in parsed.values()) and parsed['min']>=parsed['max']:return None
        result={'airspace':name,('forbiddenAirspaceBand' if action=='FORBIDDEN' else 'mandatoryAirspaceBand'):parsed}
        if action=='FORBIDDEN' and parsed['max']=='UNLIMITED':
            extensions=self.ptr_terminal_extensions(state.get('condition',{}))
            if extensions is None:return None
            if extensions:result['extensionTerminals']=extensions
        return result

    def route_profile_adjustment(self,state):
        routing=state.get('routing',{});seq=routing.get('sequences',[])
        if set(routing)-{'sequences'} or len(seq)!=1 or set(seq[0])-{'elements'} or len(seq[0].get('elements',[]))!=1:return None
        e=seq[0]['elements'][0];cls=kind(e)
        if cls not in ('RoutingElementRouteIdRelevant','RoutingElementPointList','RoutingElementDctSegment'):return None
        if set(e)-{'@class','positionedObject1','positionedObject2','routeId','verticalLimits'}:return None
        refs=[r for key in ('includedLocation','secondLocation')
              for cell in state.get('condition',{}).get(key,{}).get('elements',[]) if cell.get('referenceLocation') is True
              for r in cell.get('crossingLocation',[])]
        if not refs or any(kind(r).replace('CrossingLocationElement','RoutingElement')!=cls
            or any(identifier(r.get(k,{}))!=identifier(e.get(k,{})) for k in ('positionedObject1','positionedObject2','routeId')) for r in refs):return None
        limits=e.get('verticalLimits',{});bounds=[]
        if set(limits)!={'min','max'}:return None
        for key,unbounded in (('min','GROUND'),('max','UNLIMITED')):
            b=limits[key]
            if b.get('discriminator')=='FL' and type(b.get('FL')) is int and 0<=b['FL']<=999 and not(set(b)-{'@class','discriminator','FL'}):bounds.append(b['FL']*100)
            elif b.get('discriminator')=='LIMIT' and b.get('LIMIT')==unbounded and not(set(b)-{'@class','discriminator','LIMIT'}):bounds.append(None)
            else:return None
        if bounds==[None,None] or (None not in bounds and bounds[0]>bounds[1]):return None
        action=state.get('restrictionType')
        if action not in ('MANDATORY','FORBIDDEN'):return None
        if action=='FORBIDDEN' and bounds[1] is None and self.ptr_terminal_extensions(state.get('condition',{}))!=[]:return None
        location=self.element({k:v for k,v in e.items() if k!='verticalLimits'})
        return {'routeLocation':location,'mode':action,'minFt':bounds[0],'maxFt':bounds[1]}

    def grouped_profile_adjustment(self,state):
        action=state.get('restrictionType');routing=state.get('routing',{})
        refs=[e for key in ('includedLocation','secondLocation')
              for cell in state.get('condition',{}).get(key,{}).get('elements',[]) if cell.get('referenceLocation') is True
              for e in cell.get('crossingLocation',[])]
        if action=='CLOSED' and 'routing' not in state:
            action='FORBIDDEN';routing={'sequences':[{'elements':[{**e,'@class':e['@class'].replace('CrossingLocationElement','RoutingElement')}]} for e in refs]}
        seq=routing.get('sequences',[])
        if action not in ('FORBIDDEN','MANDATORY') or set(routing)-{'sequences'} or not seq:return None
        if len(seq)==1 and state.get('restrictionType')!='CLOSED':return None
        if any(set(s)-{'elements'} or len(s.get('elements',[]))!=1 for s in seq):return None
        def signature(e):return (kind(e).replace('CrossingLocationElement','RoutingElement'),*(identifier(e.get(k,{})) for k in ('id','routeId','positionedObject1','positionedObject2')))
        elements=[s['elements'][0] for s in seq]
        if not refs or {signature(e) for e in refs}!={signature(e) for e in elements}:return None
        choices=[]
        for e in elements:
            derived=copy.deepcopy(state);derived['restrictionType']=action;derived['routing']={'sequences':[{'elements':[e]}]}
            for key in ('includedLocation','secondLocation'):
                for cell in derived.get('condition',{}).get(key,{}).get('elements',[]):
                    if cell.get('referenceLocation') is True:
                        cell['crossingLocation']=[x for x in cell.get('crossingLocation',[]) if signature(x)==signature(e)]
                        if not cell['crossingLocation']:return None
            adjustment=self.point_profile_adjustment(derived) or self.airspace_profile_adjustment(derived) or self.route_profile_adjustment(derived)
            if adjustment is None:return None
            choices.append({'scope':self.flow(derived['condition']),'schedule':self.applicability(derived),
                'referenceLocation':self.reference_location(derived),'sourceValidity':derived['period'],'adjustment':adjustment})
        return {'profileChoices':choices,'mode':action}

    def terminal_dct_allowances(self,state,terminal):
        if 'routing' not in state:return []
        routing=state['routing'];airport=identifier(state.get('condition',{}).get('aerodromeId',{}))
        if not airport or set(routing)-{'sequences'} or not routing.get('sequences'):return None
        allowed=[]
        for sequence in routing['sequences']:
            if set(sequence)-{'elements'} or len(sequence.get('elements',[]))!=1:return None
            e=sequence['elements'][0]
            if kind(e)!='RoutingElementDctSegment' or set(e)-{'@class','positionedObject1','positionedObject2','verticalLimits'}:return None
            a=identifier(e.get('positionedObject1',{}));b=identifier(e.get('positionedObject2',{}))
            if not a or not b or (a if terminal=='dep' else b)!=airport:return None
            if 'verticalLimits' in e:
                limits=e['verticalLimits']
                if set(limits)!={'min','max'}:return None
                for k,v in (('min','GROUND'),('max','UNLIMITED')):
                    bound=limits[k]
                    if set(bound)-{'@class','discriminator','LIMIT'} or bound.get('discriminator')!='LIMIT' or bound.get('LIMIT')!=v:return None
            allowed.append(b if terminal=='dep' else a)
        return sorted(set(allowed))

    def cell(self, value):
        cls = kind(value)
        if set(value) - {'@class', 'referenceLocation', 'operand', 'crossingLocation', 'terminalLocation'}:
            return self.unknown('unhandledCellFields', value)
        if cls == 'LocationConditionGroupElementCrossing':
            elements = value.get('crossingLocation', [])
            return combine('or', [self.element(x, value.get('referenceLocation') is True) for x in elements]) if elements else self.unknown('emptyCrossingCell', value)
        terminal = {'LocationConditionGroupElementTerminalDeparting': 'dep',
                    'LocationConditionGroupElementTerminalArriving': 'arr'}
        if cls in terminal:
            elements = value.get('terminalLocation', [])
            if any(set(e) - {'@class', 'id', 'type'} for e in elements):
                return self.unknown('unhandledTerminalFields', value)
            if any('type' in e and e['type'] != e.get('id', {}).get('discriminator') for e in elements):
                return self.unknown('terminalTypeMismatch', value)
            return combine('or', [self.entity(terminal[cls], x.get('id', {})) for x in elements]) if elements else self.unknown('emptyTerminalCell', value)
        return self.unknown('conditionCell:' + cls, value)

    def location_group(self, value):
        if set(value) - {'elements'}:
            return self.unknown('unhandledLocationGroupFields', value)
        elements = value.get('elements', [])
        if not elements:
            return self.unknown('emptyLocationGroup', value)
        operands = {e.get('operand', 'AND') for e in elements[1:]}
        if operands <= {'AND', 'AND_THEN'}:
            op = 'ordered'
        elif operands == {'OR'}:
            op = 'or'
        else:
            return self.unknown('mixedLocationOperands', value)
        return combine(op, [self.cell(e) for e in elements])

    def property(self, value):
        cls = kind(value)
        allowed = {'@class', 'operator', 'conditions'} if cls == 'GroupCondition' else {'@class', 'value'}
        if set(value) - allowed:
            return self.unknown('unhandledPropertyFields', value)
        if cls == 'GroupCondition':
            children = [self.property(x) for x in value.get('conditions', [])]
            op = value.get('operator')
            if op == 'NOT' and len(children) == 1:
                return negate(children[0])
            if op in ('AND', 'OR') and children:
                return combine(op.lower(), children)
            return self.unknown('propertyBoolean', value)
        if cls == 'FlightTypeCondition':
            code = {'GENERAL': 'G', 'MILITARY': 'M', 'NON_SCHEDULED': 'N',
                    'NOT_SCHEDULED': 'N', 'SCHEDULED': 'S', 'OTHER': 'X'}.get(value.get('value'))
            if code:
                return {'op': 'flightType', 'values': [code]}
        if cls == 'AircraftTypeCondition':
            return self.entity('aircraft', value.get('value', {}))
        if cls == 'AircraftTypeClassificationCondition' and value.get('value') in ('??J', '??T', '??P'):
            return {'op': 'engineType', 'values': [value['value'][-1]]}
        if cls == 'AircraftTypeClassificationCondition' and isinstance(value.get('value'), str):
            pattern = value['value']
            alphabets = ('LSAHGT?*', '123468C?*', 'PTJE?*')
            if len(pattern) == 3 and all(c in allowed for c, allowed in zip(pattern, alphabets)):
                return {'op': 'aircraftClassification', 'values': [pattern]}
        if cls == 'AircraftOperatorIdCondition' and isinstance(value.get('value'), dict):
            ref = value['value']
            if kind(ref) == 'UnitRef' and isinstance(ref.get('unitId'), str) and ref['unitId']:
                return {'op': 'aircraftOperator', 'values': [ref['unitId']], 'sourceOperatorRef': ref}
        if cls in ('AEPbnCondition', 'FlightStatusCondition') and isinstance(value.get('value'), str):
            return {'op': 'pbn' if cls == 'AEPbnCondition' else 'flightStatus', 'values': [value['value']]}
        native = {'AircraftRegistrationCondition': 'registration',
                  'AEComNavCondition': 'comNavEquipment', 'AESurCondition': 'surveillanceEquipment'}
        if cls in native and isinstance(value.get('value'), str):
            code = value['value']
            # Native registrations such as EW* / RA* select the national
            # registration prefix (confirmed by the accompanying source rules).
            # Other wildcard constructions retain their diagnostic.
            if (cls == 'AircraftRegistrationCondition' and code.endswith('*')
                    and code[:-1] and code[:-1].isascii() and code[:-1].isalnum()
                    and code==code.upper()):
                return {'op':'registrationPrefix','values':[code[:-1]],'sourcePattern':code}
            valid = code and code.isascii() and code.isalnum() and code == code.upper()
            if cls != 'AircraftRegistrationCondition':
                valid = valid and code[0].isalpha() and (len(code) == 1 or (len(code) == 2 and code[1].isdigit()))
            if valid:
                return {'op': native[cls], 'values': [code]}
        return self.unknown('property:' + cls, value)

    def flow(self, value):
        if kind(value) != 'HSPTRestrictionCondition':
            return self.unknown('flow:' + kind(value), value)
        node = self.location_group(value['includedLocation']) if 'includedLocation' in value else TRUE
        if 'secondLocation' in value:
            second = self.location_group(value['secondLocation'])
            op = value.get('logicalOperandLocation')
            if op in ('AND', 'OR', 'AND_NOT'):
                node = combine('or' if op == 'OR' else 'and', [node, negate(second) if op == 'AND_NOT' else second])
            else:
                node = combine('and', [node, self.unknown('secondLocationOperand', value)])
        if 'condition' in value:
            node = combine('and', [node, self.property(value['condition'])])
        extras = set(value) - {'@class', 'includedLocation', 'secondLocation', 'logicalOperandLocation', 'condition'}
        if extras:
            node = self.unknown('unhandledFlowFields', value)
        return node

    def routing(self, value):
        if set(value) - {'sequences'}:
            return self.unknown('unhandledRoutingFields', value)
        sequences = value.get('sequences', [])
        if any(set(s) - {'elements'} for s in sequences):
            return self.unknown('unhandledRoutingSequenceFields', value)
        if not sequences or any(not s.get('elements') for s in sequences):
            return self.unknown('missingRouting', value)
        return combine('or', [combine('ordered', [self.element(e) for e in s['elements']]) for s in sequences])

    def applicability(self, state):
        if (kind(state)=='AerodromeFlightRuleRestrictionState'
                and state.get('condition',{}).get('terminalType')=='DEPARTURE_ARRIVAL'):
            # Each terminal clause below carries its own calendar and validity.
            return TRUE
        dependent = state.get('dependentApplicability')
        if dependent:
            allowed = {'@class','fua','fuaDefaultActive','fuaRad','dependentVerticalLimits','extendedVerticalLimits','condition'}
            if (set(dependent)-allowed or dependent.get('fuaRad') or dependent.get('extendedVerticalLimits')
                    or any(type(dependent.get(k)) is not bool for k in
                           ('fua','fuaDefaultActive','fuaRad','dependentVerticalLimits','extendedVerticalLimits'))):
                return self.unknown('dependentApplicabilityFlags', dependent)
            activation = self.dependent_condition(dependent.get('condition',{}), dependent)
            basic_state = {k:v for k,v in state.items() if k != 'dependentApplicability'}
            # Expanded dependent applicability already includes allocations;
            # use an explicitly H24 basic definition as the neutral calendar.
            basic = self.basic_calendar(state)
            if basic is None:
                basic = TRUE if self.basic_is_h24(state) else (
                    self.applicability(basic_state) if state.get('applicabilities') or state.get('eventApplicabilities') else TRUE)
            if basic.get('op') == 'timeAt':
                basic = basic['schedule']
            return {'op':'timeAt','location':self.reference_location(state),
                    'schedule':combine('and',[basic,activation])}
        # The expanded calendars are authoritative only for their finite
        # expansion horizon. Accept H24 only when that horizon covers the whole
        # part of this state intersecting the requested AIRAC.
        p = state['period']
        start, end = max(START, p['start']), min(END, p['end'])
        apps = state.get('applicabilities', [])
        # NMP's applicabilities are the effective expansion, already complemented
        # for negative basic applicability. eventApplicabilities retain the input
        # periods. Applying NOT again would reverse the native effective hours.
        expanded_flag = type(state.get('duringApplicability')) is bool
        if expanded_flag and not state.get('dependentApplicability'):
            for app in apps:
                weekly = app.get('weeklyDayPeriodPattern', {})
                frame = weekly.get('timeFrame', {})
                altitude = app.get('altitudeRange', {})
                unbounded = (altitude.get('min', {}).get('LIMIT') == 'MIN_NOT_DEFINED'
                             and altitude.get('max', {}).get('LIMIT') == 'MAX_NOT_DEFINED')
                if unbounded and frame.get('start', END) <= start and frame.get('end', START) >= end:
                    for period in weekly.get('timePattern', []):
                        if (kind(period) == 'WeeklyDayPeriod' and set(period.get('weekDays', [])) == set(DAYS)
                                and period.get('wef') == 0 and period.get('til') == 0
                                and set(period) <= {'@class', 'weekDays', 'wef', 'til'}
                                and set(weekly) <= {'timeFrame', 'timePattern'}
                                and set(app) <= {'weeklyDayPeriodPattern', 'altitudeRange'}):
                            return TRUE
        if (apps and not state.get('dependentApplicability')
                and expanded_flag
                and max(a.get('weeklyDayPeriodPattern', {}).get('timeFrame', {}).get('end', 0) for a in apps) >= end):
            calendars = []
            for app in apps:
                weekly = app.get('weeklyDayPeriodPattern', {})
                frame = weekly.get('timeFrame', {})
                altitude = app.get('altitudeRange', {})
                if (altitude.get('min', {}).get('LIMIT') != 'MIN_NOT_DEFINED'
                        or altitude.get('max', {}).get('LIMIT') != 'MAX_NOT_DEFINED'
                        or set(app) - {'weeklyDayPeriodPattern', 'altitudeRange'}
                        or set(weekly) - {'timeFrame', 'timePattern'}
                        or not frame.get('start') or not frame.get('end')
                        or frame['start'] % 86400000 or frame['end'] % 86400000
                        or frame['start'] >= frame['end']):
                    break
                patterns = []
                for period in weekly.get('timePattern', []):
                    if (kind(period) != 'WeeklyDayPeriod'
                            or set(period) - {'@class', 'weekDays', 'wef', 'til'}
                            or not period.get('weekDays')
                            or not set(period['weekDays']) <= set(DAYS)
                            or (period.get('wef') == period.get('til') and period.get('wef') != 0)
                            or any(not isinstance(period.get(k), int) or not 0 <= period[k] <= 86400000 or period[k] % 60000 for k in ('wef', 'til'))):
                        break
                    patterns.append({'op': 'weekSchedule', 'days': [DAYS.index(d) for d in period['weekDays']],
                                     'schedule': {'op': 'time', 'start': period['wef'] // 60000,
                                                  'end': period['til'] // 60000}})
                else:
                    if patterns:
                        calendars.append(combine('and', [
                            {'op': 'dates', 'start': iso_day(frame['start']), 'end': iso_day(frame['end'] - 1)},
                            combine('or', patterns)]))
                        continue
                break
            else:
                condition = state.get('condition', {})
                references = [cell for key in ('includedLocation', 'secondLocation')
                              for cell in condition.get(key, {}).get('elements', [])
                              if cell.get('referenceLocation') is True]
                if kind(condition) in ('TpAircraftTypeRestrictionCondition', 'AerodromeFlightRuleRestrictionCondition'):
                    terminal_classes = {
                        'DEPARTURE': ['Departing'], 'ARRIVAL': ['Arriving']}
                    # Both terminals need separate correlated rule branches:
                    # a departure predicate must not use arrival applicability.
                    references = [{'@class': 'LocationConditionGroupElementTerminal' + phase,
                                   'terminalLocation': [{'id': condition.get('aerodrome', {})}]} 
                                  for phase in terminal_classes.get(condition.get('terminalType'), [])]
                exact_references = all(
                    kind(c) in ('LocationConditionGroupElementTerminalDeparting', 'LocationConditionGroupElementTerminalArriving')
                    or (kind(c) == 'LocationConditionGroupElementCrossing'
                        and all(kind(e) in ('CrossingLocationElementPoint', 'CrossingLocationElementAirspace',
                                           'CrossingLocationElementDctSegment', 'CrossingLocationElementAirspaceBorder',
                                           'CrossingLocationElementRouteIdRelevant', 'CrossingLocationElementPointList',
                                           'CrossingLocationElementTerminalProcedure')
                                for e in c.get('crossingLocation', []))) for c in references)
                if references and calendars and exact_references:
                    # timeAt evaluates at the reference object, including ARR
                    # landing time. It does not use departure time as a proxy.
                    return {'op': 'timeAt', 'location': self.reference_location(state),
                            'schedule': combine('or', calendars)}
        # Other calendars need reference-location time and level correlation,
        # not an EOBT approximation. Preserve both forms for implementation.
        basic = self.basic_calendar(state)
        if basic is not None:
            return {'op':'timeAt','location':self.reference_location(state),'schedule':basic}
        return self.unknown('referenceLocationApplicability', {
            k: state[k] for k in ('applicabilities', 'eventApplicabilities',
                                 'duringApplicability', 'dependentApplicability') if k in state})

    def basic_calendar(self,state):
        events=state.get('eventApplicabilities',[])
        if not events or type(state.get('duringApplicability')) is not bool:
            return None
        result=[]
        for event in events:
            if set(event)-{'timeFrame','specialDayPeriod'}:return None
            frame=event.get('timeFrame',{});special=event.get('specialDayPeriod',{})
            if set(special)-{'timePattern','wefExtTimeInDay','tilExtTimeInDay','excludeTimePattern'} or special.get('excludeTimePattern') is not False:
                return None
            pattern=special.get('timePattern',{});period=pattern.get('period',{})
            if (set(pattern)!={'period'} or kind(period)!='WeeklyDayPeriod'
                    or set(period)-{'@class','weekDays','wef','til'}
                    or not period.get('weekDays') or not set(period['weekDays'])<=set(DAYS)
                    or any(type(period.get(k)) is not int or not 0<=period[k]<=86400000 or period[k]%60000 for k in ('wef','til'))
                    or (period['wef']==period['til'] and period['wef']!=0)):
                return None
            for key,expected in (('wefExtTimeInDay',period['wef']),('tilExtTimeInDay',period['til'] or 86400000)):
                value=special.get(key,{})
                if (set(value)-{'@class','timeReference','offset'} or value.get('timeReference')!='MIDNIGHT'
                        or value.get('offset')!=expected):return None
            if frame.get('discriminator')=='ExtendedDatesPair':
                pair=frame.get('ExtendedDatesPair',{});low,high=pair.get('wef',{}),pair.get('til',{})
                if (set(frame)-{'@class','discriminator','ExtendedDatesPair'} or set(pair)-{'@class','wef','til'}
                        or any(set(v)-{'@class','discriminator','Concrete'} or v.get('discriminator')!='Concrete'
                               or type(v.get('Concrete')) is not int for v in (low,high))
                        or low['Concrete']>=high['Concrete']):return None
                date={'op':'utcInterval','start':low['Concrete'],'end':high['Concrete']}
                # This flag requests a CACD timetable-update job. It does not
                # request a second runtime UTC offset: keep the explicit native
                # dates and hours exactly as captured, including expired periods.
            elif (frame.get('discriminator')=='Season' and frame.get('Season') in ('Summer','Winter')
                  and not(set(frame)-{'@class','discriminator','Season'})):
                date={'op':'season','value':frame['Season'].upper()}
            else:return None
            week={'op':'weekSchedule','days':[DAYS.index(d) for d in period['weekDays']],
                  'schedule':{'op':'time','start':period['wef']//60000,'end':period['til']//60000}}
            result.append(combine('and',[date,week]))
        node=combine('or',result)
        return node if state['duringApplicability'] else negate(node)

    def dependent_condition(self, value, flags):
        cls=kind(value)
        if cls=='DependentApplicabilityRestrictionActivation' and not(set(value)-{'@class','activations'}):
            periods=[]
            if not isinstance(value.get('activations'),list):return self.unknown('restrictionActivationShape',value)
            for item in value['activations']:
                if (set(item)-{'@class','startTime','endTime','remark','fmp','confirmed'}
                    or item.get('confirmed')!='SAVED_CONFIRMED'
                    or any(type(item.get(k)) is not int for k in ('startTime','endTime'))
                    or item['startTime']>=item['endTime']):return self.unknown('restrictionActivationConfirmation',item)
                periods.append({'op':'utcInterval','start':item['startTime'],'end':item['endTime']})
            return combine('or',periods)
        if (cls=='DependentApplicabilityRouteAvailability'
            and not(set(value)-{'@class','route','fromPoint','toPoint','offsetWef','offsetTil','verticalLimits'})
            and all(type(value.get(k,0)) is int and -90<=value.get(k,0)<=90 for k in ('offsetWef','offsetTil'))):
            def ref(v):return v.get(v.get('discriminator'),v).get('ref',{})
            route=ref(value.get('route',{}));a=ref(value.get('fromPoint',{}));b=ref(value.get('toPoint',{}))
            limits=value.get('verticalLimits',{});bounds=[]
            if all(r.get('backendId') and r.get('uuid') for r in (route,a,b)) and set(limits)=={'min','max'}:
                for key,unbounded in (('min','GROUND'),('max','UNLIMITED')):
                    bound=limits[key]
                    if bound.get('discriminator')=='FL' and type(bound.get('FL')) is int and 0<=bound['FL']<=999 and not(set(bound)-{'@class','discriminator','FL'}):bounds.append(bound['FL']*100)
                    elif bound.get('discriminator')=='LIMIT' and bound.get('LIMIT')==unbounded and not(set(bound)-{'@class','discriminator','LIMIT'}):bounds.append(0 if key=='min' else None)
                    else:break
                if len(bounds)==2 and (bounds[1] is None or bounds[0]<=bounds[1]):
                    return {'op':'nativeRouteAvailability','sourceRouteRef':route,'fromPointRef':a,'toPointRef':b,
                        'contextKey':'|'.join(r['backendId'] for r in (route,a,b)),
                        'minFt':bounds[0],'maxFt':bounds[1],
                        'offsetStartMinutes':value.get('offsetWef',0),'offsetEndMinutes':value.get('offsetTil',0)}
        if cls == 'DAConditionGroup' and not(set(value)-{'@class','operator','conditions'}):
            children=[self.dependent_condition(v,flags) for v in value.get('conditions',[])]
            if value.get('operator')=='NOT' and len(children)==1:
                return negate(children[0])
            if value.get('operator') in ('AND','OR') and children:
                return combine(value['operator'].lower(),children)
        if (cls == 'DependentApplicabilityAirspaceActivation'
                and not(set(value)-{'@class','airspace','offsetWef','offsetTil'})
                and all(type(value.get(k,0)) is int for k in ('offsetWef','offsetTil'))):
            ref=value.get('airspace',{}).get('ref',{})
            if ref.get('backendId') and ref.get('airspaceId'):
                return {'op':'nativeActivation','sourceRef':ref,
                        'offsetStartMinutes':value.get('offsetWef',0),'offsetEndMinutes':value.get('offsetTil',0),
                        'vertical':flags['dependentVerticalLimits'],
                        'requested':self.vertical_reference=='RFL','fua':flags['fua']}
        return self.unknown('dependentCondition:'+cls,value)

    def basic_is_h24(self,state):
        if state.get('duringApplicability') is not True:
            return False
        for event in state.get('eventApplicabilities',[]):
            if set(event)-{'timeFrame','specialDayPeriod'}:
                continue
            frame=event.get('timeFrame',{})
            pair=frame.get('ExtendedDatesPair',{})
            low,high=pair.get('wef',{}),pair.get('til',{})
            special=event.get('specialDayPeriod',{})
            pattern=special.get('timePattern',{})
            period=pattern.get('period',{})
            if (frame.get('discriminator')=='ExtendedDatesPair'
                    and set(frame)<={'@class','discriminator','ExtendedDatesPair'}
                    and set(pair)<={'@class','wef','til'}
                    and all(set(b)<={'@class','discriminator','Concrete'} and b.get('discriminator')=='Concrete' for b in (low,high))
                    and type(low.get('Concrete')) is int and type(high.get('Concrete')) is int
                    and low['Concrete']<=self.window[0] and high['Concrete']>=self.window[1]
                    and set(special)<={'timePattern','wefExtTimeInDay','tilExtTimeInDay','excludeTimePattern'}
                    and special.get('excludeTimePattern') is False
                    and set(pattern)=={'period'} and kind(period)=='WeeklyDayPeriod'
                    and set(period)<={'@class','weekDays','wef','til'}
                    and set(period.get('weekDays',[]))==set(DAYS)
                    and period.get('wef')==0 and period.get('til')==0
                    and all(set(special.get(k,{}))<={'@class','timeReference','offset'}
                            and special.get(k,{}).get('timeReference')=='MIDNIGHT'
                            and special[k].get('offset')==offset
                            for k,offset in (('wefExtTimeInDay',0),('tilExtTimeInDay',86400000)))):
                return True
        return False

    def dct_band(self,limits):
        if not limits:return {}
        if set(limits)!={'min','max'}:return None
        result={}
        for key,symbol in [('min','GROUND'),('max','UNLIMITED')]:
            b=limits[key]
            if b.get('discriminator')=='LIMIT' and b.get('LIMIT')==symbol and not(set(b)-{'@class','discriminator','LIMIT'}):continue
            if b.get('discriminator')!='FL' or type(b.get('FL')) is not int or not 0<=b['FL']<=999 or set(b)-{'@class','discriminator','FL'}:return None
            result[key+'Ft']=b['FL']*100
        if result.get('minFt',0)>result.get('maxFt',float('inf')):return None
        return result

    def enroute_dct(self,state):
        c=state.get('condition',{});action=state.get('restrictionType')
        if kind(state)!='DctLimitRestrictionState' or kind(c)!='CrossingDctLimitRestrictionCondition' or action not in ('MANDATORY','FORBIDDEN'):return None
        if set(c)-{'@class','airspaceId','dctLimit','verticalLimits','condition'}:return None
        name=identifier(c.get('airspaceId',{}));band=self.dct_band(c.get('verticalLimits',{}));limit=c.get('dctLimit')
        if not name or band is None or (limit is not None and (type(limit) not in (int,float) or not 0<=limit<=998)):return None
        if action=='FORBIDDEN' and limit is not None:return None
        routing=state.get('routing',{});segments=[]
        if set(routing)-{'sequences'}:return None
        for seq in routing.get('sequences',[]):
            if set(seq)!={'elements'} or len(seq['elements'])!=1:return None
            e=seq['elements'][0];bounds=self.dct_band(e.get('verticalLimits',{}))
            if kind(e)!='RoutingElementDctSegment' or set(e)-{'@class','positionedObject1','positionedObject2','verticalLimits'} or bounds is None:return None
            a,b=(identifier(e.get(k,{})) for k in ('positionedObject1','positionedObject2'))
            if not a or not b:return None
            segments.append({'from':a,'to':b,**bounds})
        if action=='FORBIDDEN' and not segments:return None
        return {'op':'nativeDctViolation','airspace':name,'crossingBand':band,'segmentOptions':segments,'mode':action,'limitNm':limit}

    def fra_border(self,state):
        c=state.get('condition',{})
        if kind(state) not in ('FraDctLimitRestrictionState','DctLimitRestrictionState') or kind(c)!='CrossingBorderDctLimitRestrictionCondition':return None
        if state.get('restrictionType')!='MANDATORY' or c.get('dctLimit')!=0:return None
        if set(c)-{'@class','dctLimit','crossingBorderAirspaceIds','condition','verticalLimits'}:return None
        band=self.dct_band(c.get('verticalLimits',{}))
        if band is None:return None
        if state.get('enableFraPtGeneration') not in (None,False):return None
        borders=[]
        for pair in c.get('crossingBorderAirspaceIds',[]):
            if set(pair)!={'fromAirspaceId','toAirspaceId'}:return None
            a,b=(identifier(pair[k]) for k in ('fromAirspaceId','toAirspaceId'))
            if not a or not b or a==b:return None
            borders.append({'from':a,'to':b})
        if not borders:return None
        routing=state.get('routing',{})
        if set(routing)!={'sequences'} or not routing['sequences']:return None
        points=[];segments=[];point_options=[];segment_options=[]
        for seq in routing['sequences']:
            if set(seq)!={'elements'} or len(seq['elements'])!=1:return None
            e=seq['elements'][0]
            bounds=self.dct_band(e.get('verticalLimits',{}))
            if bounds is None:return None
            if kind(state)=='FraDctLimitRestrictionState' and kind(e)=='RoutingElementPoint' and set(e)<={'@class','id','fraType','verticalLimits'} and e.get('fraType')=='ENTRY_EXIT':
                name=identifier(e.get('id',{}))
                if not name:return None
                if bounds:point_options.append({'point':name,**bounds})
                else:points.append(name)
            elif kind(e)=='RoutingElementDctSegment' and set(e)<={'@class','positionedObject1','positionedObject2','verticalLimits'}:
                a,b=(identifier(e.get(k,{})) for k in ('positionedObject1','positionedObject2'))
                if not a or not b:return None
                if bounds:segment_options.append({'from':a,'to':b,**bounds})
                else:segments.append([a,b])
            else:return None
        result={'op':'fraBorderViolation','borders':borders,'entryExitPoints':points,'allowedDctSegments':segments}
        if band:result['crossingBand']=band
        if point_options:result['pointOptions']=point_options
        if segment_options:result['segmentOptions']=segment_options
        return result

    def state(self, record, state, row):
        self.issues = []
        self.source_paths = {}
        def index_paths(value, path=''):
            if isinstance(value, (dict, list)):
                self.source_paths[id(value)] = path
                items = value.items() if isinstance(value, dict) else enumerate(value)
                for key, child in items:
                    index_paths(child, path + '/' + str(key).replace('~', '~0').replace('/', '~1'))
        index_paths(state)
        self.window = (max(START, state['period']['start']), min(END, state['period']['end']))
        entity = record['data']
        ref = entity['ref']
        cls = kind(state)
        self.state_class = cls
        self.vertical_reference = 'RFL' if cls=='PTRestrictionState' else state.get('verticalLimitReference', 'CFL')
        condition = state.get('condition', {})
        fra=self.fra_border(state) or self.enroute_dct(state)
        terminal_ops = {'DEPARTURE': ['dep'], 'ARRIVAL': ['arr'], 'DEPARTURE_ARRIVAL': ['dep', 'arr']}
        aerodrome_rule = cls == 'AerodromeFlightRuleRestrictionState' and kind(condition) == 'AerodromeFlightRuleRestrictionCondition'
        procedure_rule = cls == 'TpAircraftTypeRestrictionState' and kind(condition) == 'TpAircraftTypeRestrictionCondition'
        dct_terminal={'TerminalDepartingDctLimitRestrictionCondition':'dep','TerminalArrivingDctLimitRestrictionCondition':'arr'}.get(kind(condition)) if cls=='DctLimitRestrictionState' else None
        terminals = terminal_ops.get(condition.get('terminalType'), []) if aerodrome_rule or procedure_rule else []
        if fra:
            scope=self.property(condition['condition']) if 'condition' in condition else TRUE
        elif dct_terminal:
            scope=self.entity(dct_terminal,condition.get('aerodromeId',{}))
            if set(condition)-{'@class','aerodromeId','dctLimit','condition'}:
                scope=self.unknown('unhandledTerminalDctFields',condition)
            elif 'condition' in condition:
                scope=combine('and',[scope,self.property(condition['condition'])])
        elif (aerodrome_rule or procedure_rule) and terminals:
            scope = combine('or', [self.entity(op, condition.get('aerodrome', {})) for op in terminals])
            if procedure_rule:
                if set(condition) - {'@class', 'condition', 'terminalType', 'aerodrome'}:
                    scope = self.unknown('unhandledTerminalProcedureCondition', condition)
                else:
                    scope = combine('and', [scope, self.property(condition.get('condition', {}))])
        else:
            scope = self.flow(condition) if kind(condition) == 'HSPTRestrictionCondition' else self.unknown('condition:' + kind(condition), condition)
        action = state.get('restrictionType')
        profile_adjustment=(self.point_profile_adjustment(state) or self.airspace_profile_adjustment(state) or self.route_profile_adjustment(state) or self.grouped_profile_adjustment(state)) if cls=='PTRestrictionState' else None
        routing = self.routing(state.get('routing', {})) if 'routing' in state and not profile_adjustment and not fra else None
        dct_allowances=self.terminal_dct_allowances(state,dct_terminal) if dct_terminal else None
        if fra:
            predicate=fra
        elif (dct_terminal and action=='MANDATORY' and dct_allowances is not None
                and type(condition.get('dctLimit')) in (int,float) and 0<=condition['dctLimit']<=998):
            predicate={'op':'terminalDctLength','terminal':dct_terminal,'value':condition['dctLimit']}
            if dct_allowances:predicate['allowedPoints']=dct_allowances
        elif aerodrome_rule and terminals and action == 'FORBIDDEN' and not (set(condition) - {'@class', 'terminalType', 'aerodrome'}):
            predicate = combine('or', [combine('and', [
                self.entity(op, condition.get('aerodrome', {})),
                {'op': 'flightRulesAtTerminal', 'terminal': op}]) for op in terminals])
            if len(terminals)==2:
                branches=[]
                for op,terminal in (('dep','DEPARTURE'),('arr','ARRIVAL')):
                    derived={**state,'condition':{**condition,'terminalType':terminal}}
                    phase_schedule=self.applicability(derived)
                    if phase_schedule.get('op')=='timeAt':phase_schedule=phase_schedule['schedule']
                    location=self.entity(op,condition.get('aerodrome',{}))
                    branches.append(combine('and',[location,{'op':'flightRulesAtTerminal','terminal':op},
                        {'op':'timeAt','location':location,'schedule':combine('and',[phase_schedule,
                            {'op':'utcInterval','start':state['period']['start'],'end':state['period']['end']}])}]))
                predicate=combine('or',branches)
        elif cls == 'HSRestrictionState' and action == 'CLOSED' and routing is None:
            predicate = TRUE
        elif cls=='HSRestrictionState' and action=='CLOSED_FOR_CRUISING' and routing is None:
            refs=[e for key in ('includedLocation','secondLocation')
                  for cell in condition.get(key,{}).get('elements',[]) if cell.get('referenceLocation') is True
                  for e in cell.get('crossingLocation',[])]
            valid={'CrossingLocationElementAirspace','CrossingLocationElementRouteIdRelevant',
                   'CrossingLocationElementPointList','CrossingLocationElementDctSegment'}
            predicate=({'op':'cruiseAt','location':self.reference_location(state,whole=True)}
                       if refs and all(kind(e) in valid for e in refs)
                       else self.unknown('cruiseReferenceLocation',state))
        elif (cls == 'HSRestrictionState' and action in ('CLOSED_FOR_ENTRY_BY_DCT','CLOSED_FOR_EXIT_BY_DCT')
                and routing is None and self.reference_points_only(state)):
            predicate={'op':'dctAtReference','direction':'entry' if action=='CLOSED_FOR_ENTRY_BY_DCT' else 'exit',
                       'location':self.reference_location(state)}
        elif (cls == 'HSRestrictionState' or procedure_rule) and action in ('FORBIDDEN', 'MANDATORY') and routing:
            predicate = routing if action == 'FORBIDDEN' else negate(routing)
        elif cls == 'PTRestrictionState':
            # PTR adjusts the calculated profile; it must never be translated
            # into a direct FPL rejection just because its routing says forbidden.
            predicate = TRUE if profile_adjustment else self.unknown('profileAdjustment', state)
        else:
            predicate = self.unknown('restrictionAction:' + str(action), state)
        schedule = self.applicability(state)
        if fra:
            predicate['schedule']=schedule.get('schedule',schedule) if schedule.get('op')=='timeAt' else schedule
            predicate['sourceValidity']=state['period']
        guards = []
        for field in ('airborneOnly', 'dynamicallyActivated',
                      'fraArrivalDeparture', 'aupRad', 'aupRadDefaultActive'):
            if state.get(field):
                guards.append(self.unknown(field, state[field]))
        if state.get('ioatPortion') not in (None, 'NON_IOAT'):
            guards.append(self.unknown('ioatPortion', state['ioatPortion']))
        if state.get('hsclassification') not in (None, 'H'):
            guards.append(self.unknown('softRestriction', state['hsclassification']))
        allowed = {'@class', 'period', 'stateId', 'condition', 'applicabilities',
                   'duringApplicability', 'enabled', 'eventApplicabilities',
                   'operationalGoal', 'restrictionType', 'textualDescription',
                   'autoDSTChangeEnabled', 'externallyAvailable', 'airborneOnly',
                   'dynamicallyActivated', 'ioatPortion', 'routing',
                   'verticalLimitReference', 'fraArrivalDeparture', 'aupRad',
                   'aupRadDefaultActive', 'hsclassification', 'dependentApplicability'}
        if set(state) - allowed:
            extras=set(state)-allowed-({'enableFraPtGeneration'} if fra else set())
            if extras:guards.append(self.unknown('unhandledStateFields', {k: state[k] for k in extras}))
        period = state['period']
        enabled = state.get('enabled')
        if enabled not in ('Enabled_TRUE', 'Enabled_FALSE'):
            guards.append(self.unknown('enabledState', enabled))
        reference = self.reference_location(state)
        rule = {
            'key': 'NMP:' + ref['backendId'] + ':' + state['stateId'],
            'id': ref['restrictionId'], 'sheet': 'NMP:' + ref['backendId'], 'row': row,
            'validFrom': iso_day(period['start']), 'validUntil': iso_day(period['end'] - 1),
            'change': 'SUS' if enabled == 'Enabled_FALSE' else '',
            'scope': scope, 'schedule': schedule,
            'referenceLocation': reference,
            'sourceValidity': {'start': period['start'], 'end': period['end']},
            'clauses': [{'action': 'forbidden', 'condition': combine('and', [predicate] + guards)}],
            'text': state.get('textualDescription', state.get('operationalGoal', '')),
            'refs': [], 'sourceRef': ref, 'sourceState': state,
            'sourceProcessing': record['summary']['flightPlanProcessing'],
            'effect': 'profileAdjustment' if cls == 'PTRestrictionState' else (
                'messageProcessing' if cls in ('ErrorManagementRestrictionState', 'MessageMonitoringRestrictionState') else 'flightRestriction'),
            'conversionDiagnostics': list(self.issues),
        }
        if profile_adjustment:
            rule['profileAdjustments']=[profile_adjustment]
            rule['clauses'][0]['action']='profileAdjustment'
        return rule

    def reference_points_only(self,state):
        condition=state.get('condition',{})
        cells=[c for key in ('includedLocation','secondLocation') for c in condition.get(key,{}).get('elements',[])
               if c.get('referenceLocation') is True]
        return bool(cells) and all(kind(c)=='LocationConditionGroupElementCrossing'
            and c.get('crossingLocation') and all(kind(e)=='CrossingLocationElementPoint' for e in c['crossingLocation']) for c in cells)

    def reference_location(self, state, whole=False):
        condition = state.get('condition', {})
        fra=self.fra_border(state) or self.enroute_dct(state)
        if fra:
            if fra['op']=='nativeDctViolation':return {'op':'dctArea','airspace':fra['airspace'],'crossingBand':fra['crossingBand']}
            return {'op':'fraBorder','borders':fra['borders'],**({'crossingBand':fra['crossingBand']} if 'crossingBand' in fra else {})}
        dct_terminal={'TerminalDepartingDctLimitRestrictionCondition':'dep','TerminalArrivingDctLimitRestrictionCondition':'arr'}.get(kind(condition))
        if dct_terminal and kind(state)=='DctLimitRestrictionState':
            return self.entity(dct_terminal,condition.get('aerodromeId',{}))
        if kind(condition)=='AerodromeFlightRuleRestrictionCondition' and condition.get('terminalType')=='DEPARTURE_ARRIVAL':
            return combine('or',[self.entity(op,condition.get('aerodrome',{})) for op in ('dep','arr')])
        if kind(condition) in ('AerodromeFlightRuleRestrictionCondition', 'TpAircraftTypeRestrictionCondition'):
            op = {'DEPARTURE': 'dep', 'ARRIVAL': 'arr'}.get(condition.get('terminalType'))
            if op:
                return self.entity(op, condition.get('aerodrome', {}))
        cells = [cell for key in ('includedLocation', 'secondLocation')
                 for cell in condition.get(key, {}).get('elements', [])
                 if cell.get('referenceLocation') is True]
        supported = {'CrossingLocationElementPoint', 'CrossingLocationElementAirspace',
                     'CrossingLocationElementDctSegment', 'CrossingLocationElementAirspaceBorder',
                     'CrossingLocationElementRouteIdRelevant', 'CrossingLocationElementPointList',
                     'CrossingLocationElementTerminalProcedure'}
        if cells and all(kind(c) in ('LocationConditionGroupElementTerminalDeparting',
                                    'LocationConditionGroupElementTerminalArriving')
                         or (kind(c) == 'LocationConditionGroupElementCrossing'
                             and all(kind(e) in supported for e in c.get('crossingLocation', [])))
                         for c in cells):
            nodes=[]
            for cell in cells:
                if set(cell)-{'@class','referenceLocation','operand','crossingLocation','terminalLocation'}:
                    nodes.append(self.unknown('unhandledCellFields',cell))
                elif not whole and kind(cell)=='LocationConditionGroupElementCrossing' and any(
                        kind(e) in ('CrossingLocationElementRouteIdRelevant','CrossingLocationElementPointList','CrossingLocationElementTerminalProcedure') for e in cell.get('crossingLocation',[])):
                    nodes.append(combine('or',[{'op':'firstSegment','location':self.element(e,True)}
                        if kind(e) in ('CrossingLocationElementRouteIdRelevant','CrossingLocationElementPointList','CrossingLocationElementTerminalProcedure') else self.element(e,True)
                        for e in cell['crossingLocation']]))
                else:nodes.append(self.cell(cell))
            return combine('or',nodes)
        return self.unknown('stateValidityReference', condition)


def compile_captures(paths, dependencies=None):
    records, expected, sources = {}, {}, []
    rules, compiler = [], Compiler(dependencies)
    for path in paths:
        raw = path.read_bytes()
        data = json.loads(raw)
        if data.get('schema') != 'eurocontrol-rs-capture/1' or data.get('branch') != 'production' or data.get('referenceStart') != START:
            raise ValueError('Mixed or unsupported capture metadata: ' + str(path))
        sources.append({'file': path.name, 'sha256': hashlib.sha256(raw).hexdigest()})
        for wrapper in data.get('index', []):
            summary = wrapper['RESTRICTION']
            key = summary['ref']['backendId']
            if key in expected and expected[key] != summary:
                raise ValueError('Conflicting index: ' + key)
            expected[key] = summary
        for record in data['records']:
            if record['data'].get('ref') != record['summary'].get('ref'):
                raise ValueError('Restriction identity differs from its summary: ' + str(path))
            key = record['data']['ref']['backendId']
            digest = hashlib.sha256(json.dumps({'summary': record['summary'], 'data': record['data']}, sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest()
            if key in records and records[key][0] != digest:
                raise ValueError('Conflicting capture: ' + key)
            if key in records:
                continue
            records[key] = (digest, record['summary'])
            seen = set()
            for state in record['data']['states']:
                if state['stateId'] in seen:
                    raise ValueError('Duplicate state: ' + key)
                seen.add(state['stateId'])
                p = state['period']
                if p['start'] >= p['end']:
                    raise ValueError('Invalid state interval: ' + key)
                if p['start'] < END and p['end'] > START:
                    rules.append(compiler.state(record, state, 0))
    if not expected or set(records) - set(expected):
        raise ValueError('Missing source index or unexpected restriction')
    for key, (_, summary) in records.items():
        if summary != expected[key]:
            raise ValueError('Summary differs from captured index: ' + key)
    rules.sort(key=lambda r: r['key'])
    for row, rule in enumerate(rules):
        rule['row'] = row
    diagnostics = collections.Counter(d['reason'] for r in rules for d in r['conversionDiagnostics'])
    missing = sorted(set(expected) - set(records))
    return {
        'schema': 'iatfm-rad/1', 'airac': '2611', 'version': 'NMP-candidate',
        'sourceKind': 'nmp-database',
        'validFrom': iso_day(START), 'validUntil': iso_day(END),
        'validationStatus': 'incomplete', 'operationallyValidated': False,
        'globalPendingValidation': [
            'Reference-location time must govern state selection across midnight.',
            'Imported predicates require source-to-runtime differential verification.',
            'All geographic and navigation references must be resolved for this AIRAC.',
            'Sequential live captures must be checked against a consistent source revision.',
            'Native PTRs affect local profile rendering and FPL checks; propagation into network-wide CASA traffic counts remains to be validated.',
        ],
        'sourceFiles': sources, 'groups': [], 'rules': rules,
        'sourceDependenciesSha256': hashlib.sha256(json.dumps(dependencies or {},
            sort_keys=True, separators=(',', ':')).encode('utf-8')).hexdigest(),
        'sourceDependencies': dependencies or {},
        'summary': {'expectedRestrictions': len(expected), 'capturedRestrictions': len(records),
                    'missingRestrictions': len(missing), 'rules': len(rules),
                    'disabledRules': sum(r['change'] == 'SUS' for r in rules),
                    'rulesWithPendingSemantics': sum(bool(r['conversionDiagnostics']) for r in rules),
                    'nonDisabledRulesWithPendingSemantics': sum(
                        r['change'] != 'SUS' and bool(r['conversionDiagnostics']) for r in rules),
                    'pendingSemantics': dict(diagnostics)},
        'missingBackendIds': missing,
    }


def publication_blockers(candidate):
    summary = candidate.get('summary', {})
    blockers = []
    if candidate.get('schema') != 'iatfm-rad/1' or candidate.get('sourceKind') != 'nmp-database':
        blockers.append('invalidNativeCatalogue')
    if summary.get('missingRestrictions') != 0:
        blockers.append('missingRestrictions')
    def unresolved(value):
        if isinstance(value, dict):
            return value.get('op') == 'unknown' or any(unresolved(v) for v in value.values())
        return isinstance(value, list) and any(unresolved(v) for v in value)
    rules = candidate.get('rules', [])
    # Recheck executable expressions: summary counters alone are not evidence
    # that a stale or externally edited candidate is ready to run.
    actual_pending = any(r.get('conversionDiagnostics') or any(
        unresolved(r.get(k)) for k in ('scope', 'schedule', 'referenceLocation', 'clauses')) for r in rules)
    if summary.get('rulesWithPendingSemantics') != 0 or actual_pending:
        blockers.append('pendingSemantics')
    if not candidate.get('rules') or summary.get('rules') != len(candidate['rules']):
        blockers.append('ruleCountMismatch')
    if candidate.get('operationallyValidated') is not True or candidate.get('globalPendingValidation'):
        blockers.append('runtimeValidationPending')
    return blockers


def integration_report(candidate):
    def input_kinds(value):
        result=set()
        if isinstance(value,dict):
            op=value.get('op')
            if op in ('nativeActivation','nativeRouteAvailability','aircraftClassification','aircraftOperator'):
                result.add(op)
            for v in value.values():result.update(input_kinds(v))
        elif isinstance(value,list):
            for v in value:result.update(input_kinds(v))
        return result
    inputs=collections.Counter(kind for rule in candidate['rules'] for kind in input_kinds(
        {k:rule.get(k) for k in ('scope','schedule','referenceLocation','clauses','profileAdjustments')}))
    affected = collections.Counter(reason for rule in candidate['rules']
        for reason in {d['reason'] for d in rule['conversionDiagnostics']})
    summary = candidate['summary']
    return {'schema':'iatfm-rad-integration-report/1', 'airac':candidate['airac'],
            'summary':summary, 'sourceFiles':candidate['sourceFiles'],
            'sourceDependenciesSha256':candidate['sourceDependenciesSha256'],
            'rulesWithoutPendingSemantics':summary['rules']-summary['rulesWithPendingSemantics'],
            'rulesRequiringRuntimeInputs':dict(inputs),
            'rulesAffectedByReason':dict(affected.most_common()),
            'publicationBlockers':publication_blockers(candidate),
            'globalPendingValidation':candidate['globalPendingValidation']}


def write_json_atomic(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(value, stream, ensure_ascii=False, separators=(',', ':'))
    temporary.replace(path)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publish', action='store_true', help='Publish only a complete, validated native catalogue')
    args = parser.parse_args()
    root = Path(__file__).parent
    dependencies_path = root / 'database-2611-dependencies.json'
    dependencies = json.loads(dependencies_path.read_text(encoding='utf-8'))['entities'] if dependencies_path.exists() else {}
    candidate = compile_captures(sorted(root.glob('database-2611-part-*.json')), dependencies)
    output = root / 'catalogue.database.candidate.json'
    write_json_atomic(output, candidate)
    write_json_atomic(root / 'database-integration-report.json', integration_report(candidate))
    if args.publish:
        blockers = publication_blockers(candidate)
        if blockers:
            raise SystemExit('Native publication blocked: ' + ', '.join(blockers))
        write_json_atomic(root / 'catalogue.database.active.json', candidate)
    print(json.dumps(candidate['summary']))
