"""Produce a reproducible French coverage report from one candidate snapshot."""
import collections
import json
from datetime import datetime, timezone
from pathlib import Path
from import_database import integration_report, write_json_atomic

CAUSES = {
    'localVerticalLimits': "Limites de procédures ou des familles DCT/FRA non encore évaluées complètement. Les tranches ordinaires et inversées GND–FLOOR / CEILING–UNL sont prises en charge pour HS et les conditions PTR sur RFL, avec composition des blocs superposés.",
    'fraPointRole': "Les frontières dirigées avec points ENTRY_EXIT et bandes numériques inclusives sont prises en charge et corrélées au DCT franchissant. Restent les points intermédiaires FRA en route, les rôles séparés et les combinaisons non reconnues ; un simple passage au point est insuffisant.",
    'stateValidityReference': "Le type de lieu de référence n'a pas encore de traduction temporelle complète, notamment dans les familles DCT/FRA. Les portions ATS et SID/STAR utilisent maintenant leur premier segment lorsque la navdata permet de le résoudre.",
    'referenceLocationApplicability': "Calendrier non traduit intégralement : dates spéciales, expressions étendues, calendrier absent ou horizon d'expansion insuffisant.",
    'profileAdjustment': "Combinaison PTR encore non prise en charge : séquences de plusieurs lieux ordonnés, extension terminale d'une portion ATS/DCT, bornes inversées de l'ajustement ou références incompatibles. Les points, portions ATS/DCT, volumes obligatoires, extensions terminales des volumes interdits et alternatives simples sont raccordés au profil local et contrôlés après recalcul. Une PTR ne provoque jamais directement un rejet du plan de vol.",
    'profileLocation': "Condition PTR sur un type de lieu encore non couvert, notamment procédure, frontière ou DCT. Les points, volumes ordinaires et portions ATS utilisées partiellement sont évalués sur le RFL ; cela ne suffit pas à traduire l'ajustement vertical associé.",
    'cruiseReferenceLocation': "Une fermeture à la croisière doit porter sur une portion de route ou un volume. Un point isolé ou un autre type de référence ne permet pas de mesurer une phase de vol en palier.",
    'unresolvedAerodromeSet': "Le groupe référencé n'est pas encore présent avec la même identité interne et la bonne validité dans les dépendances sauvegardées.",
    'dependentApplicabilityFlags': "Combinaison FUA RAD, limites étendues ou indicateurs d'applicabilité non prise en charge ; aucune valeur par défaut n'est substituée.",
    'dependentCondition:DependentApplicabilityRouteAvailability': "Forme de disponibilité ATS/CDR non reconnue. L'opérateur est implémenté pour les références exactes et les fenêtres datées complètes de chaque segment ; une donnée absente reste indéterminée à l'exécution.",
    'dependentCondition:DependentApplicabilityRestrictionActivation': "Forme de périodes d'activation non reconnue. Les périodes directement incluses dans la restriction et confirmées SAVED_CONFIRMED sont traduites en intervalles UTC ; ce champ n'est pas une référence à une autre restriction.",
    'restrictionActivationConfirmation': "Période d'activation non confirmée SAVED_CONFIRMED ; elle ne peut pas être traitée comme une période publiée.",
    'dependentCondition:': "La définition dépendante ne fournit pas une condition reconnue ; interprétation impossible sans sa sémantique native.",
    'ioatPortion': "La sélection de la portion iOAT/GAT du vol reste à implémenter.",
    'aupRad': "L'activation propre à la restriction via AUP/UUP reste à relier au contexte daté.",
    'unhandledStateFields': "Des champs sémantiques supplémentaires de l'état natif ne sont pas encore interprétés ; l'état original est conservé.",
    'unhandledPropertyFields': "La propriété possède des paramètres supplémentaires ; ils ne sont pas ignorés pour produire artificiellement un résultat complet.",
    'enabledState': "Valeur d'activation différente des deux valeurs explicitement reconnues Enabled_TRUE/Enabled_FALSE.",
    'airborneOnly': "La restriction est réservée au profil CTFM d'un vol activé. La sélection selon l'état opérationnel en vol n'est pas raccordée au calcul local des profils prévol.",
    'aerodromeSetValidity': "Aucun état unique du groupe d'aérodromes ne couvre tout l'intervalle de l'état RAD. Il faut découper la règle aux changements de composition du groupe.",
    'aerodromeSetFields': "La définition du groupe comporte des paramètres supplémentaires non interprétés ; sa liste de membres ne peut pas être supposée complète.",
    'implicitAerodromePattern': "Le motif implicite du groupe dépasse les préfixes alphabétiques explicitement pris en charge.",
    'restrictionAction:REJECT': "Action du traitement des messages IFPS : elle dépend du contexte et des erreurs du message, dont iATFM n'implémente pas le pipeline natif.",
    'restrictionAction:IGNORE': "Suppression conditionnelle d'une erreur de traitement IFPS ; il manque la correspondance avec les erreurs natives et leur contexte.",
    'restrictionAction:DELETE': "Suppression conditionnelle dans le traitement des messages IFPS ; le moteur local ne reproduit pas ce traitement.",
    'restrictionAction:FORBIDDEN_FOR_DCT': "L'interdiction doit être appliquée aux segments DCT traversant le volume concerné, avec leurs limites et exceptions. Le contrôle terminal de longueur DCT ne couvre pas cette action.",
    'restrictionAction:CLOSED_FOR_ENTRY_BY_DCT': "Le lieu de référence n'est pas un point simple. Il faut corréler le franchissement de sa frontière au segment DCT entrant.",
}

def explanation(reason):
    if reason in CAUSES: return CAUSES[reason]
    if 'PointList' in reason:
        return "La portion ATS doit être développée en une séquence géographique de points, indépendante du nom de route emprunté. Cette comparaison complète n'est pas encore implémentée."
    if reason.startswith('restrictionAction:'):
        action=reason.split(':',1)[1]
        if action in ('HIGHLIGHT','FORCE_INVALID','REPLACE','MM_FORCE_INVALID','MM_ALERT'):
            return "Action de traitement ou surveillance de messages IFPS ; iATFM ne possède pas encore le pipeline de messages correspondant."
        if action=='CLOSED_FOR_CRUISING':
            return "Nécessite de distinguer une phase de croisière d'une montée/descente au lieu restreint."
        if action=='MANDATORY':
            return "Obligation d'une famille DCT/FRA : application de limites de distance et d'exceptions différente d'une route obligatoire HS."
        return "Action native non couverte pour ce type d'état et cette combinaison de paramètres."
    if reason.startswith('condition:'):
        return "Famille de limitation DCT/FRA non traduite complètement : limite de distance, frontière ou connexion terminale et exceptions doivent être évaluées ensemble."
    if reason.startswith('property:'):
        return "Type ou syntaxe de propriété non interprété complètement (notamment motifs de texte, immatriculation ou origine du message)."
    return "Construction native non prise en charge ; voir sourceClass et sourcePointer dans le catalogue candidat."

def build(candidate):
    report=integration_report(candidate)
    details=collections.defaultdict(lambda:{'rules':set(),'activeRules':set(),'ids':set(),'classes':set()})
    for rule in candidate['rules']:
        for diagnostic in rule['conversionDiagnostics']:
            entry=details[diagnostic['reason']]
            entry['rules'].add(rule['key']);entry['ids'].add(rule['id'])
            if rule['change']!='SUS':entry['activeRules'].add(rule['key'])
            if diagnostic.get('sourceClass'):entry['classes'].add(diagnostic['sourceClass'])
    report['generatedAt']=datetime.now(timezone.utc).isoformat()
    report['missingConditions']=[{'reason':reason,'affectedStates':len(entry['rules']),
        'nonDisabledAffectedStates':len(entry['activeRules']), 'examples':sorted(entry['ids'])[:8],
        'sourceClasses':sorted(entry['classes']),'why':explanation(reason)}
        for reason,entry in sorted(details.items(),key=lambda item:(-len(item[1]['rules']),item[0]))]
    s=report['summary'];converted=report['rulesWithoutPendingSemantics'];total=s['rules']
    rate=100*converted/total if total else 0
    report['conversionPercent']=rate
    non_disabled=total-s['disabledRules']
    report['conversionPercentNonDisabled']=100*(non_disabled-s['nonDisabledRulesWithPendingSemantics'])/non_disabled if non_disabled else 0
    report['extractionPercent']=100*s['capturedRestrictions']/s['expectedRestrictions'] if s['expectedRestrictions'] else 0
    report['thresholdSatisfiedForCapturedStatesOnly']=rate>90
    report['completeExtractionAndConversionAbove90']=s['missingRestrictions']==0 and rate>90
    lines=["RAPPORT D'INTEGRATION RAD NM — AIRAC "+candidate['airac'],report['generatedAt'],"",
        f"Extraction : {s['capturedRestrictions']} / {s['expectedRestrictions']} restrictions ({report['extractionPercent']:.2f} %).",
        f"Restrictions encore absentes : {s['missingRestrictions']}.",
        f"Conversion sans diagnostic incomplet : {converted} / {total} états ({rate:.2f} %).",
        f"Conversion des seuls états non désactivés : {report['conversionPercentNonDisabled']:.2f} %.",
        "Le dénominateur inclut tous les états disponibles qui intersectent l'AIRAC, y compris les états désactivés.",
        "Les occurrences de diagnostics se recoupent : les effectifs par motif ne doivent pas être additionnés.",
        "Ce taux mesure la traduction des états AIRAC capturés ; il ne représente pas une validation opérationnelle IFPS.",
        "Le catalogue natif reste candidat. Le catalogue actif issu d'Excel n'a pas été remplacé.","",
        "DONNEES NECESSAIRES A L'EXECUTION DES OPERATEURS TRADUITS"]
    for name,count in report['rulesRequiringRuntimeInputs'].items():
        lines.append(f"- {name} : {count} états exigent un contexte daté. Sans contexte valable : résultat indéterminé.")
    lines += ["Les activations natives exigent une liste complète d'allocations, la même identité de source et, pour FUA, l'affectation explicite de la restriction.",
        "La disponibilité ATS/CDR exige nativeRouteAvailability : chaque segment doit avoir au moins un niveau ouvert, avec références natives et fenêtres datées complètes.",
        "Les espaces sont résolus depuis le référentiel local ; routes et points depuis la navdata X-Plane.",
        "La résolution exhaustive des identifiants et la comparaison avec NM restent à valider.",
        "Les PTR traduites sont raccordées aux outils locaux de profil et de vérification FPL. Leur propagation aux comptages CASA globaux reste à valider.",
        "Si un ajustement ou son extension ne respecte pas les bornes après recalcul, il est signalé comme non appliqué ; les règles ne sont pas supprimées pour augmenter le taux.","","CONDITIONS NON TRADUITES"]
    lines.insert(lines.index('CONDITIONS NON TRADUITES'),"Le choix d'un passage au-dessus ou au-dessous d'une tranche PTR produit un profil admissible dans le modèle local ; son identité avec le profil choisi par NM n'est pas démontrée.")
    for item in report['missingConditions']:
        lines += [f"- {item['reason']} : {item['affectedStates']} états, dont {item['nonDisabledAffectedStates']} non désactivés.",
                  "  Cause : "+item['why'],"  Exemples : "+", ".join(item['examples'])]
        if item['sourceClasses']:lines.append("  Types natifs : "+", ".join(item['sourceClasses']))
    lines += ["","BLOCAGES DE PUBLICATION",", ".join(report['publicationBlockers']),"",
        "Les fichiers sources et leur empreinte SHA-256 figurent dans le rapport JSON ; les états natifs sont conservés dans le candidat."]
    return report,"\n".join(lines)+"\n"

if __name__=='__main__':
    root=Path(__file__).parent
    candidate=json.loads((root/'catalogue.database.candidate.json').read_text(encoding='utf-8'))
    report,rendered=build(candidate)
    active_path=root/'catalogue.database.active.json'
    activation_path=root/'database-activation.json'
    if active_path.exists() and activation_path.exists():
        activation=json.loads(activation_path.read_text(encoding='utf-8'))
        report['nativeCatalogueSelection']=activation
        rendered=rendered.replace("Le catalogue natif reste candidat. Le catalogue actif issu d'Excel n'a pas été remplacé.",
            "Le catalogue NM est sélectionné en mode avec diagnostics. Le fichier Excel est conservé en secours ; la sélection ne constitue pas une validation opérationnelle.")
    context_path=root/'local-context.json'
    context=json.loads(context_path.read_text(encoding='utf-8')) if context_path.exists() else {}
    report['runtimeContext']={'filePresent':context_path.exists(),
        'nativeActivationRecords':len(context.get('nativeActivations',{})),
        'nativeRouteAvailabilityRecords':len(context.get('nativeRouteAvailability',{}))}
    rendered += ("\nCONTEXTE LOCAL DISPONIBLE\n"
        + ("local-context.json présent.\n" if context_path.exists() else "local-context.json absent.\n")
        + f"Enregistrements d'activation native : {report['runtimeContext']['nativeActivationRecords']}.\n"
        + f"Enregistrements de disponibilité ATS/CDR : {report['runtimeContext']['nativeRouteAvailabilityRecords']}.\n"
        + "Les opérateurs d'activation sont traduits et testés ; leur résultat opérationnel reste indéterminé sans les allocations correspondantes.\n")
    audit_path=root/'local-reference-audit.json'
    if audit_path.exists():
        audit=json.loads(audit_path.read_text(encoding='utf-8'))
        report['localReferencePresence']={k:audit[k] for k in (
            'capturedRestrictions','states','statesWithAbsentSourceIdentifiers',
            'translatedStatesWithAbsentSourceIdentifiers','spaceSourceDate','limitations')}
        report['localReferencePresence']['missingIdentifierCounts']={k:len(v) for k,v in audit['missingIdentifiers'].items()}
        rendered += ("\nPRESENCE DES REFERENCES LOCALES\n"
            + f"Audit effectué sur {audit['capturedRestrictions']} restrictions capturées, {audit['states']} états.\n"
            + f"{audit['translatedStatesWithAbsentSourceIdentifiers']} états traduits citent au moins un identifiant absent de l'inventaire local.\n"
            + "Ce contrôle mesure la présence des identifiants, pas un échec sur un vol : les alias, les coordonnées et les identifiants comparés directement peuvent être résolus autrement.\n"
            + "Les SID/STAR X-Plane sont inclus. Les identifiants absents et les règles concernées sont détaillés dans local-reference-audit.json.\n"
            + f"Date de source déclarée par le catalogue d'espaces : {audit['spaceSourceDate']}. La compatibilité avec l'AIRAC RAD doit être validée.\n")
    write_json_atomic(root/'database-integration-report.json',report)
    pending=[{'key':r['key'],'id':r['id'],'sourceClass':r['sourceState']['@class'],
              'sourceValidity':r['sourceValidity'],'disabled':r['change']=='SUS',
              'conditions':[{**d,'why':explanation(d['reason'])} for d in r['conversionDiagnostics']]}
             for r in candidate['rules'] if r['conversionDiagnostics']]
    write_json_atomic(root/'database-unconverted-rules.json',{
        'generatedAt':report['generatedAt'],'airac':candidate['airac'],
        'capturedRestrictions':report['summary']['capturedRestrictions'],
        'incompleteStates':len(pending),'rules':pending})
    rendered += "\nListe exhaustive des états incomplets et des chemins des conditions : database-unconverted-rules.json.\n"
    temporary=root/'rapport-integration-nm.tmp'
    temporary.write_text(rendered,encoding='utf-8')
    temporary.replace(root/'rapport-integration-nm.txt')
    print(json.dumps({k:report[k] for k in ('conversionPercent','extractionPercent','thresholdSatisfiedForCapturedStatesOnly')}))
