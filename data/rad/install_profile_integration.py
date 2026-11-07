"""Install checked native point-PTR integration in the local profile tools."""
from pathlib import Path
import hashlib

HERE=Path(__file__).resolve().parent
REPO=HERE.parents[1]

def edit(relative,old,new):
    path=REPO/relative
    original=path.read_bytes();text=original.decode('utf-8').replace('\r\n','\n')
    if new in text:return
    if text.count(old)!=1:raise SystemExit('Changed/ambiguous profile integration anchor: '+relative)
    backup=HERE/(path.name+'.before-profile-integration')
    if not backup.exists():backup.write_bytes(original)
    updated=text.replace(old,new)
    if b'\r\n' in original:updated=updated.replace('\n','\r\n')
    path.write_bytes(updated.encode('utf-8'))

def install(source,relative):
    target=REPO/relative;content=(HERE/source).read_bytes()
    stamp=HERE/(source+'.installed-sha256.txt')
    if target.exists() and target.read_bytes()!=content:
        if not stamp.exists() or hashlib.sha256(target.read_bytes()).hexdigest()!=stamp.read_text().strip():
            raise SystemExit('Profile integration target changed externally: '+relative)
    target.write_bytes(content);stamp.write_text(hashlib.sha256(content).hexdigest())

install('profile_core_integration.rs','crates/iatfm-core/src/dashboard/rad_native_profile.rs')
install('profile_server_integration.rs','crates/iatfm-server/src/app/rad_native_profile.rs')
profile_path=REPO/'crates/iatfm-core/src/dashboard/route_profile.rs'
profile_text=profile_path.read_text(encoding='utf-8')
if 'fn build_flight_plan_trajectory_native(' not in profile_text:
    start=profile_text.index('fn build_flight_plan_trajectory(')
    end=profile_text.index('    let flight_plan = pilot.flight_plan.as_ref()?;',start)
    signature=profile_text[start:end]
    if signature.count('loa_constraints: &[MatchedLoaConstraint],')!=1:raise SystemExit('Unexpected trajectory signature')
    wrapper=signature+'''    build_flight_plan_trajectory_native(sampled_at,pilot,route_navdata,procedure_navdata,
        active_runways_by_airport,airport_winds,performance,weather,offsets,time_labels,loa_constraints,&[])
}

'''
    native=signature.replace('fn build_flight_plan_trajectory(', 'fn build_flight_plan_trajectory_native(').replace(
        '    loa_constraints: &[MatchedLoaConstraint],','    loa_constraints: &[MatchedLoaConstraint],\n    native_constraints: &[NativeProfileConstraint],')
    edit('crates/iatfm-core/src/dashboard/route_profile.rs',signature,wrapper+native)
edit('crates/iatfm-core/src/dashboard/route_profile.rs',
    '    apply_loa_constraints_to_route_points(&mut route_points, loa_constraints);',
    '''    if !apply_native_profile_spans(&mut route_points,native_constraints,true) {return None;}
    apply_loa_constraints_to_route_points(&mut route_points, loa_constraints);''')
edit('crates/iatfm-core/src/dashboard/route_profile.rs',
    '    insert_toc_tod_points(&mut route_points, &mut profiles, &vertical_performance);',
    '''    insert_toc_tod_points(&mut route_points, &mut profiles, &vertical_performance);
    if native_constraints.iter().any(|c|c.distance_span.is_some()) {
        if !apply_native_profile_spans(&mut route_points,native_constraints,false) {return None;}
        apply_vertical_profile(&mut profiles,&route_points,flight_plan,start_altitude,vertical_performance);
    }''')
edit('crates/iatfm-core/src/dashboard/mod.rs','include!("route_profile.rs");',
     'include!("route_profile.rs");\ninclude!("rad_native_profile.rs");')
edit('crates/iatfm-server/src/app/rad_expression.rs','include!("rad_native_integration.rs");',
     'include!("rad_native_integration.rs");\ninclude!("rad_native_profile.rs");')
edit('crates/iatfm-server/src/app/rad.rs',
     '        for rule in selected.values() {\n            context.rule_key',
     '''        for rule in selected.values() {
            // Point PTRs are applied by the profile builder. They never reject
            // a flight plan as a direct traffic-flow restriction.
            if rad_str(rule,"effect")=="profileAdjustment" && rule["profileAdjustments"].is_array()
                && !rad_native_rule_pending(rule) {continue;}
            context.rule_key''')

common='''        let points = if native_constraints.is_empty() {points} else {
            match iatfm_core::dashboard::build_native_constrained_profile(
                now, PILOT, &snapshot.clients.atcs, &routes, &reference.navdata, &winds,
                &self.aircraft_performance, weather.as_deref(), &native_constraints) {
                Ok(adjusted)=>{profile_tuning["profileAdjusted"]=true.into();adjusted},
                Err(error)=>{profile_tuning["error"]=error.into();profile_tuning["profileAdjusted"]=false.into();points},
            }
        };
'''
edit('crates/iatfm-server/src/app/flight_tools.rs',
     '        let sectors =\n            iatfm_core::dashboard::build_vertical_profile_sectors(&points, &reference.sectors);',
     '''        let (native_constraints,mut profile_tuning)=self.native_profile_constraints(
            pilot,&points,&reference,self.native_profile_takeoff(pilot,now));
'''+common.replace('PILOT','pilot')+'''        let sectors =
            iatfm_core::dashboard::build_vertical_profile_sectors(&points, &reference.sectors);''')
edit('crates/iatfm-server/src/app/flight_tools.rs',
     '            "points":points, "sectors":sectors, "lateral":lateral,',
     '            "points":points, "sectors":sectors, "lateral":lateral, "profileTuning":profile_tuning,')
edit('crates/iatfm-server/src/app/flight_tools.rs',
     '        let (mut rad_checks, mut rad_metadata) = self.check_rad_and_areas(&pilot, &points, &reference, takeoff);',
     '''        let (native_constraints,mut profile_tuning)=self.native_profile_constraints(&pilot,&points,&reference,takeoff);
'''+common.replace('PILOT','&pilot')+'''        let (mut rad_checks, mut rad_metadata) = self.check_rad_and_areas(&pilot, &points, &reference, takeoff);
        if profile_tuning.get("error").is_some() || profile_tuning["pendingRules"].as_array().is_some_and(|v|!v.is_empty()) {
            rad_checks.push(rad_json!({"field":"RAD profile tuning","status":"warning",
                "message":"Some native profile constraints could not be applied; the raw profile or unresolved constraints require review.",
                "details":profile_tuning}));
        }
        rad_metadata["profileTuning"]=profile_tuning;''')
print('Installed point-PTR constraints in local profile rendering and FPL checks')
