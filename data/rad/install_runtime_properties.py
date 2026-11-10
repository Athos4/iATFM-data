"""Install the narrowly scoped, reviewed NMP property operators in the server."""
from pathlib import Path
import hashlib

root = Path(__file__).resolve().parents[2]
app = root / 'crates/iatfm-server/src/app'
expression = app / 'rad_expression.rs'
main = app / 'rad.rs'
helper = app / 'rad_database.rs'
source = Path(__file__).with_name('runtime_properties.rs').read_text(encoding='utf-8')
texts = {p: p.read_text(encoding='utf-8') for p in (expression, main)}
anchor = '            "engineType" => {'
addition = ('            "registration" | "comNavEquipment" | "surveillanceEquipment" | "flightRulesAtTerminal" => {\n'
            '                self.eval_database_property(node)\n'
            '            }\n')
if 'self.eval_database_property(node)' not in texts[expression]:
    if texts[expression].count(anchor) != 1:
        raise SystemExit('Expression anchor changed; no files written')
    texts[expression] = 'include!("rad_database.rs");\n\n' + texts[expression].replace(anchor, addition + anchor)
else:
    texts[expression] = texts[expression].replace(
        '"registration" | "comNavEquipment" | "surveillanceEquipment" => {',
        '"registration" | "comNavEquipment" | "surveillanceEquipment" | "flightRulesAtTerminal" => {')
anchor = '                | "flightPurpose" => "missingFlightAttribute",'
replacement = ('                | "flightPurpose" | "registration" | "comNavEquipment"\n'
               '                | "surveillanceEquipment" => "missingFlightAttribute",')
if replacement not in texts[main]:
    if texts[main].count(anchor) != 1 and '"flightRulesAtTerminal" => "missingFlightAttribute"' not in texts[main]:
        raise SystemExit('Diagnostic anchor changed; no files written')
    texts[main] = texts[main].replace(anchor, replacement)
texts[main] = texts[main].replace(
    '| "surveillanceEquipment" => "missingFlightAttribute",',
    '| "surveillanceEquipment" | "flightRulesAtTerminal" => "missingFlightAttribute",')
if helper.exists() and helper.read_text(encoding='utf-8') != source:
    previous = hashlib.sha256(helper.read_bytes()).hexdigest()
    if previous != '9a8433eb5b188f957df7f6cabd927b21bb163b34751c7efa0ac60c9681c93629':
        raise SystemExit('Different rad_database.rs already exists; no files written')
for path, text in texts.items():
    before = path.read_bytes()
    newline = '\r\n' if b'\r\n' in before else '\n'
    after = text.replace('\n', newline).encode('utf-8')
    if after != before:
        backup = Path(__file__).with_name(path.name + '.before-database')
        if not backup.exists():
            backup.write_bytes(before)
        path.write_bytes(after)
helper.write_text(source, encoding='utf-8', newline='\n')
print('Installed registration, comNavEquipment and surveillanceEquipment operators')
