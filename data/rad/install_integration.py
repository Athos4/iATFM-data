"""Apply narrow native-state integration changes with checked anchors/backups."""
from pathlib import Path
import hashlib

here = Path(__file__).parent
app = here.resolve().parents[1] / 'crates/iatfm-server/src/app'
paths = {name: app / name for name in ('rad.rs', 'rad_expression.rs', 'rad_tests.rs')}
texts = {name: path.read_text(encoding='utf-8') for name, path in paths.items()}

def replace(name, old, new):
    if new in texts[name]:
        return
    if texts[name].count(old) != 1:
        raise SystemExit('Changed/ambiguous anchor in ' + name + ': ' + old[:80])
    texts[name] = texts[name].replace(old, new)

replace('rad_expression.rs', 'include!("rad_database.rs");',
        'include!("rad_database.rs");\ninclude!("rad_native_integration.rs");')
replace('rad_expression.rs', '            "engineType" => {',
        '            "utcInterval" => rad_database_interval(self.at, node),\n            "engineType" => {')
if '"aircraftOperator" => self.database_attribute(node)' not in texts['rad_expression.rs']:
    replace('rad_expression.rs', '            "utcInterval" => rad_database_interval(self.at, node),',
            '            "aircraftClassification" | "aircraftOperator" => self.database_attribute(node),\n            "utcInterval" => rad_database_interval(self.at, node),')
replace('rad_expression.rs', '            "utcInterval" => rad_database_interval(self.at, node),',
        '            "airportInAirspace" => self.database_airport_in_airspace(node),\n            "utcInterval" => rad_database_interval(self.at, node),')
if '"nativeActivation" => self.database_activation(node)' not in texts['rad_expression.rs']:
    replace('rad_expression.rs', '            "airportInAirspace" => self.database_airport_in_airspace(node),',
        '            "nativeActivation" => self.database_activation(node),\n            "airportInAirspace" => self.database_airport_in_airspace(node),')
if '"dctAtReference" => self.database_dct_at_reference(node)' not in texts['rad_expression.rs']:
    replace('rad_expression.rs', '            "nativeActivation" => self.database_activation(node),',
        '            "dctAtReference" => self.database_dct_at_reference(node),\n            "nativeActivation" => self.database_activation(node),')
if '"terminalDctLength" => self.database_terminal_dct_length(node)' not in texts['rad_expression.rs']:
    replace('rad_expression.rs', '            "dctAtReference" => self.database_dct_at_reference(node),',
        '            "terminalDctLength" => self.database_terminal_dct_length(node),\n            "dctAtReference" => self.database_dct_at_reference(node),')
replace('rad_expression.rs', '            "dep" | "arr" => {',
        '            "dep" | "arr" | "airportInAirspace" => {')
replace('rad_expression.rs', '                let minute = if rad_str(node, "op") == "dep" {',
        '                let minute = if rad_str(node, "op") == "dep" || rad_str(node, "terminal") == "dep" {')
replace('rad_expression.rs', '            "scheduled" => {\n                let (e, u) = self.events(&node["condition"]);',
        '            "segmentLevel" => self.database_segment_events(node),\n            "scheduled" => {\n                let (e, u) = self.events(&node["condition"]);')
if '"firstSegment" | "airspaceFootprint" | "segmentLevel" | "ordered"' not in texts['rad_expression.rs']:
  if '"airspaceFootprint" | "segmentLevel" | "ordered"' not in texts['rad_expression.rs']:
    replace('rad_expression.rs', '            "ordered" | "boundary" | "boundaryAny" => {',
            '            "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {')
    replace('rad_expression.rs', '            "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {',
            '            "airspaceFootprint" | "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {')
  replace('rad_expression.rs', '            "airspaceFootprint" | "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {',
        '            "firstSegment" | "airspaceFootprint" | "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {')
replace('rad_expression.rs', '            "segmentLevel" => self.database_segment_events(node),',
        '            "airspaceFootprint" => self.database_airspace_footprint_events(node),\n            "segmentLevel" => self.database_segment_events(node),')
replace('rad_expression.rs','            "airspaceFootprint" => self.database_airspace_footprint_events(node),',
        '            "firstSegment" => self.database_first_segment_events(node),\n            "airspaceFootprint" => self.database_airspace_footprint_events(node),')
replace('rad_expression.rs', '                        self.sample_seconds(start, end)\n                    };',
        '                        self.database_schedule_samples(start, end, &node["schedule"])\n                    };')
replace('rad.rs', '        for rule in &cat.rules {\n            let start = rad_str(rule, "validFrom");',
'''        for rule in &cat.rules {
            if rule.get("sourceValidity").is_some() {
                let arrival = points.last().and_then(|p| p.minute_offset)
                    .map(|m| takeoff + ChronoDuration::minutes(i64::from(m)));
                if rad_database_state_overlaps(rule, takeoff, arrival) {
                    selected.insert((rad_str(rule, "sheet").into(), rad_str(rule, "key").into()), rule);
                }
                continue;
            }
            let start = rad_str(rule, "validFrom");''')
replace('rad.rs', '                && rad_str(&rule["schedule"], "op") != "true";',
        '                && !rad_schedule_has_own_reference(&rule["schedule"]);')
replace('rad.rs', '                let schedule = if time_unknown {',
'''                let schedule = if rule.get("sourceValidity").is_some() {
                    context.database_schedule(rule)
                } else if time_unknown {''')
replace('rad.rs', 'struct RadCache {', 'struct RadCache {\n    source_path: Option<std::path::PathBuf>,')
if 'global_pending_validation: Vec<String>,' not in texts['rad.rs']:
 replace('rad.rs', '    rules: Vec<RadValue>,\n}', '''    rules: Vec<RadValue>,
    #[serde(default)]
    source_kind: String,
    #[serde(default)]
    summary: RadValue,
    #[serde(default)]
    operationally_validated: bool,
    #[serde(default)]
    global_pending_validation: Vec<String>,
}''')
replace('rad.rs', '        let path = self.data_dir.join("rad/catalogue.json");',
'''        let native = self.data_dir.join("rad/catalogue.database.active.json");
        let native_mode = native.is_file();
        let path = if native_mode { native } else { self.data_dir.join("rad/catalogue.json") };''')
replace('rad.rs', '        if cache.modified == Some(modified) {',
        '        if cache.modified == Some(modified) && cache.source_path.as_ref() == Some(&path) {')
replace('rad.rs', '                if c.schema == "iatfm-rad/1" {',
'''                if native_mode && (c.source_kind != "nmp-database"
                    || !c.operationally_validated || !c.global_pending_validation.is_empty()
                    || c.summary["missingRestrictions"].as_u64() != Some(0)
                    || c.summary["rulesWithPendingSemantics"].as_u64() != Some(0)
                    || c.summary["rules"].as_u64() != Some(c.rules.len() as u64)
                    || c.rules.is_empty()) {
                    return Err("Native RAD catalogue is incomplete or not validated".into());
                }
                if c.schema == "iatfm-rad/1" {''')
replace('rad.rs', '        cache.modified = Some(modified);',
        '        cache.modified = Some(modified);\n        cache.source_path = Some(path);')
replace('rad.rs', '                if native_mode && (c.source_kind != "nmp-database"',
'''                if native_mode && c.rules.iter().any(rad_native_rule_pending) {
                    return Err("Native RAD catalogue contains unresolved expressions".into());
                }
                if native_mode && (c.source_kind != "nmp-database"''')
replace('rad_expression.rs', '            "firstSegment" => self.database_first_segment_events(node),',
        '            "routePointList" => self.database_point_list_events(node),\n            "firstSegment" => self.database_first_segment_events(node),')
if '"airspaceSlice" | "routePointList"' not in texts['rad_expression.rs']:
    replace('rad_expression.rs', '            "firstSegment" | "airspaceFootprint" | "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {',
            '            "routePointList" | "firstSegment" | "airspaceFootprint" | "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {')
replace('rad_expression.rs', '            "terminalDctLength" => self.database_terminal_dct_length(node),',
        '            "registrationPrefix" => self.database_registration_prefix(node),\n            "terminalDctLength" => self.database_terminal_dct_length(node),')
replace('rad.rs', '                | "flightPurpose" | "registration" | "comNavEquipment"',
        '                | "flightPurpose" | "registrationPrefix" | "registration" | "comNavEquipment"')
replace('rad_expression.rs', '            "routePointList" => self.database_point_list_events(node),',
        '            "airspaceSlice" => self.database_relative_slice_events(node),\n            "routePointList" => self.database_point_list_events(node),')
if '"procedureSpan" | "airspaceSlice"' not in texts['rad_expression.rs']:
    replace('rad_expression.rs', '            "routePointList" | "firstSegment" | "airspaceFootprint" | "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {',
            '            "airspaceSlice" | "routePointList" | "firstSegment" | "airspaceFootprint" | "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {')
replace('rad_expression.rs', '            "airspaceSlice" => self.database_relative_slice_events(node),',
        '            "procedureSpan" => self.database_procedure_events(node,false),\n            "airspaceSlice" => self.database_relative_slice_events(node),')
replace('rad_expression.rs', '            "airspaceSlice" | "routePointList" | "firstSegment" | "airspaceFootprint" | "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {',
        '            "procedureSpan" | "airspaceSlice" | "routePointList" | "firstSegment" | "airspaceFootprint" | "segmentLevel" | "ordered" | "boundary" | "boundaryAny" => {')
replace('rad.rs', '                "via" | "boundary" | "boundaryAny" | "dep" | "arr" => "unresolvedReference",',
        '                "airspaceSlice" | "airspaceFootprint" | "airportInAirspace" | "via" | "boundary" | "boundaryAny" | "dep" | "arr" => "unresolvedReference",')
replace('rad.rs', '                "active" | "activeAtLevel" => "unconfirmedActivation",',
        '                "nativeActivation" | "active" | "activeAtLevel" => "unconfirmedActivation",')
replace('rad.rs', '                "ordered" | "route" | "sequence" => "incompleteTrajectory",',
        '                "routePointList" | "firstSegment" | "segmentLevel" | "dctAtReference" | "terminalDctLength" | "ordered" | "route" | "sequence" => "incompleteTrajectory",')
replace('rad.rs', '                "engineType" | "aircraft" | "pbn" | "flightType" | "flightRules"',
        '                "aircraftClassification" | "aircraftOperator" | "engineType" | "aircraft" | "pbn" | "flightType" | "flightRules"')
replace('rad.rs', '                "nativeActivation" | "active" | "activeAtLevel" => "unconfirmedActivation",',
        '                "procedureSpan" => "incompleteTrajectory",\n                "nativeActivation" | "active" | "activeAtLevel" => "unconfirmedActivation",')

replace('rad_expression.rs', '            "registrationPrefix" => self.database_registration_prefix(node),',
        '            "cruiseAt" => self.database_cruise_at(node),\n            "registrationPrefix" => self.database_registration_prefix(node),')
replace('rad.rs', '                "procedureSpan" => "incompleteTrajectory",',
        '                "cruiseAt" => "incompleteTrajectory",\n                "procedureSpan" => "incompleteTrajectory",')

replace('rad_expression.rs', '            "nativeActivation" => self.database_activation(node),',
        '            "nativeRouteAvailability" => self.database_route_availability(node),\n            "nativeActivation" => self.database_activation(node),')
replace('rad.rs', '                "cruiseAt" => "incompleteTrajectory",',
        '                "nativeRouteAvailability" => "unconfirmedActivation",\n                "cruiseAt" => "incompleteTrajectory",')
replace('rad_expression.rs', '            "nativeRouteAvailability" => self.database_route_availability(node),',
        '            "fraBorderViolation" => self.database_fra_border_violation(node),\n            "nativeRouteAvailability" => self.database_route_availability(node),')
replace('rad_expression.rs', '            "procedureSpan" => self.database_procedure_events(node,false),',
        '            "fraBorder" => self.database_fra_border_events(node),\n            "procedureSpan" => self.database_procedure_events(node,false),')
replace('rad_expression.rs', '            "fraBorderViolation" => self.database_fra_border_violation(node),',
        '            "nativeDctViolation" => self.database_fra_border_violation(node),\n            "fraBorderViolation" => self.database_fra_border_violation(node),')
replace('rad_expression.rs', '            "fraBorder" => self.database_fra_border_events(node),',
        '            "dctArea" => self.database_dct_area_events(node),\n            "fraBorder" => self.database_fra_border_events(node),')
replace('rad.rs', '    source_kind: String,', '    source_kind: String,\n    #[serde(default)]\n    allow_incomplete_native: bool,')
replace('rad.rs', '                if native_mode && c.rules.iter().any(rad_native_rule_pending) {', '''                if native_mode && c.allow_incomplete_native && (c.source_kind != "nmp-database"
                    || c.summary["missingRestrictions"].as_u64() != Some(0)
                    || c.summary["rules"].as_u64() != Some(c.rules.len() as u64)
                    || c.summary["rulesWithPendingSemantics"].as_u64() != Some(c.rules.iter().filter(|r|rad_native_rule_pending(r)).count() as u64)
                    || c.rules.is_empty()) {
                    return Err("Native diagnostic catalogue has inconsistent counts or missing captures".into());
                }
                let native_mode = native_mode && !c.allow_incomplete_native;
                if native_mode && c.rules.iter().any(rad_native_rule_pending) {''')
replace('rad.rs', '        metadata["version"] = cat.version.clone().into();', '''        metadata["version"] = cat.version.clone().into();
        metadata["sourceKind"] = cat.source_kind.clone().into();
        metadata["operationallyValidated"] = cat.operationally_validated.into();
        metadata["allowIncompleteNative"] = cat.allow_incomplete_native.into();
        metadata["catalogueSummary"] = cat.summary.clone();''')
replace('rad.rs', '            match violation {\n                RadTruth::Yes => {', '''            if cat.source_kind == "nmp-database" && rad_native_rule_pending(rule) && scope != RadTruth::No {
                violation = RadTruth::Unknown;
                context.unresolved.insert("unsupportedSyntax".into());
            }
            match violation {
                RadTruth::Yes => {''')
test_include = 'include!("rad_native_regression_tests.rs");'
if test_include not in texts['rad_tests.rs']:
    texts['rad_tests.rs'] += '\n' + test_include + '\n'
test_source = (here / 'runtime_integration_tests.rs').read_bytes()
test_target = app / 'rad_native_regression_tests.rs'
test_stamp = here / 'installed-integration-tests-sha256.txt'
if test_target.exists() and test_target.read_bytes() != test_source:
    prior = test_target.read_text(encoding='utf-8')
    normalized = test_source.decode('utf-8').replace('static RAD_NATIVE_TEST_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());\n\n', '').replace('    let _guard = RAD_NATIVE_TEST_LOCK.lock().unwrap();\n', '')
    known = test_stamp.exists() and hashlib.sha256(test_target.read_bytes()).hexdigest() == test_stamp.read_text().strip()
    if prior != normalized and not known:
        raise SystemExit('Native regression test file changed; review before replacing')

helper = app / 'rad_native_integration.rs'
source = (here / 'runtime_integration.rs').read_bytes()
stamp = here / 'installed-integration-sha256.txt'
if helper.exists() and helper.read_bytes() != source:
    if not stamp.exists() or hashlib.sha256(helper.read_bytes()).hexdigest() != stamp.read_text().strip():
        raise SystemExit('Native integration helper changed outside this installer')
for name, path in paths.items():
    original = path.read_bytes()
    newline = '\r\n' if b'\r\n' in original else '\n'
    updated = texts[name].replace('\n', newline).encode('utf-8')
    if updated != original:
        backup = here / (name + '.before-native-integration')
        if not backup.exists():
            backup.write_bytes(original)
        path.write_bytes(updated)
helper.write_bytes(source)
test_target.write_bytes(test_source)
test_stamp.write_text(hashlib.sha256(test_source).hexdigest())
stamp.write_text(hashlib.sha256(source).hexdigest())
print('Installed native validity selection and reference-time scheduling')
