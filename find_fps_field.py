import re, io, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

raw = open(r'C:\dumper-7\5.6.1-44394996+++UE5+Release-5.6-Chameleon\Dumpspace\ClassesInfo.json', encoding='utf-8', errors='replace').read()

# Check AGameStateBase for time fields
for cls in ['AGameStateBase', 'AGameState', 'ABP_GameState_cLeon_C']:
    idx = raw.find('"' + cls + '":[')
    if idx < 0:
        continue
    end_idx = raw.find(']},{"', idx + 5)
    block = raw[idx:end_idx]
    matches = re.findall(r'"(\w*[Tt]ime\w*|[Tt]ick\w*|[Ff]rame\w*|[Ff][Pp][Ss]\w*)":\[\["(float|double)","D".*?\],(\d+)', block)
    print(f"=== {cls} time/tick/fps fields ===")
    for name, typ, off in sorted(matches, key=lambda x: int(x[2])):
        print(f"  0x{int(off):X}  {typ}  {name}")
    if not matches:
        print("  (none found)")
    print()
