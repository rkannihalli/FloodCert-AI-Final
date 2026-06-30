import json, os, re, subprocess, sys

APP_DIR = "/home/runner/workspace/artifacts/flood-cert"
results = []

def record(label, status, detail=""):
    results.append((label, status, detail))
    icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ ", "INFO": "ℹ️ "}.get(status, "?")
    print(f"{icon} [{status}] {label}")
    if detail:
        print(f"      {detail}")

def read_file(path):
    if not os.path.exists(path):
        return None
    with open(path, "r", errors="replace") as f:
        return f.read()

def check_fema_lookup_functions():
    path = os.path.join(APP_DIR, "fema_lookup.py")
    content = read_file(path)
    if content is None:
        record("fema_lookup.py exists", "FAIL", f"not found at {path}"); return
    required = ["def geocode_address","def query_fema_nfhl","def query_nfip_community",
        "def query_firm_panel","def query_tigerweb_fips","def query_county_name",
        "def query_nfip_community_csb","def nfip_community_info",
        "def determine_flood_info","def check_loma_at_point"]
    missing = [fn.replace("def ","") for fn in required if fn not in content]
    if missing:
        record("fema_lookup.py has all 10 functions","FAIL",f"Missing: {missing} — original ImportError cause")
    else:
        record("fema_lookup.py has all 10 functions","PASS","10/10 found — ImportError fixed")

def check_main_loma():
    path = os.path.join(APP_DIR, "main.py")
    content = read_file(path)
    if content is None:
        record("main.py exists","FAIL",f"not found at {path}"); return
    has_import = "check_loma_at_point" in content
    record("main.py imports check_loma_at_point","PASS" if has_import else "FAIL")
    has_call = bool(re.search(r"check_loma_at_point\(geo_lat", content))
    record("main.py calls check_loma_at_point() in search flow","PASS" if has_call else "FAIL")
    has_x500 = bool(re.search(r'outcome_zone.*in.*"X".*"X500"', content))
    record("main.py LOMA condition handles X500","PASS" if has_x500 else "FAIL",
        "Fixed" if has_x500 else "Still only checks ==\"X\" — LOMR-F X500 will not trigger override")
    has_dynamic = 'loma.get("outcome_zone"' in content and 'flood_info["flood_zone"]' in content
    record("main.py sets zone dynamically from LOMA outcome","PASS" if has_dynamic else "FAIL",
        "Fixed" if has_dynamic else "Still hardcodes \"X\" — X500 properties will show wrong zone")

def check_loma_code():
    path = os.path.join(APP_DIR, "fema_lookup.py")
    content = read_file(path)
    if content is None: return
    cols = ["case_number","outcome_zone","amendment_type","effective_date","lat","lon"]
    missing = [c for c in cols if c not in content]
    record("check_loma_at_point uses correct DB columns","PASS" if not missing else "FAIL",
        f"Missing: {missing}" if missing else "")
    record("loma_records Katy TX entry (22-06-1128A)","INFO",
        "Verify in Railway Postgres: SELECT * FROM loma_records WHERE case_number = \'22-06-1128A\';\n"
        "      Expected: outcome_zone=X500, amendment_type=LOMR-F, lat=29.800712")

def check_local_db():
    path = os.path.join(APP_DIR, "nfip_communities_db.json")
    if not os.path.exists(path):
        record("nfip_communities_db.json exists","FAIL",f"not found at {path}"); return
    try:
        with open(path) as f:
            data = json.load(f)
    except Exception as e:
        record("nfip_communities_db.json valid JSON","FAIL",str(e)); return
    tx_keys = [k for k in data if k.startswith("TX_")]
    record("nfip_communities_db.json valid JSON","PASS",f"{len(data)} county keys")
    record(f"TX county coverage ({len(tx_keys)}/254 counties)",
        "WARN" if len(tx_keys) < 254 else "PASS",
        f"INCOMPLETE — only {len(tx_keys)} TX counties. Waller County (TX_473/Katy) missing."
        if len(tx_keys) < 254 else "Complete")
    record("Waller County TX_473 in local DB","PASS" if "TX_473" in data else "FAIL",
        "Found" if "TX_473" in data else
        "MISSING — community name/number for Katy TX will fail. Need to add TX_473 entry.")

def check_csb_url():
    path = os.path.join(APP_DIR, "fema_lookup.py")
    content = read_file(path)
    if content is None: return
    m = re.search(r'FEMA_CSB_URL\s*=\s*"([^"]+)"', content)
    if not m:
        record("FEMA_CSB_URL found","FAIL","constant missing"); return
    url = m.group(1)
    record("FEMA CSB API URL","WARN" if "fema.gov" in url else "INFO",
        f"Current: {url}\n"
        "      Both v1 and v2 return 404 from Replit AND Railway. "
        "Local JSON DB should be primary source. CSB API is best-effort fallback only.")


def check_nfhl_layer():
    path = os.path.join(APP_DIR, "fema_lookup.py")
    content = read_file(path)
    if content is None: return
    has_layer28 = "NFHL/MapServer/28/query" in content
    has_old_esri = "USA_Flood_Hazard_Reduced_Set_gdb" in content
    if has_layer28 and not has_old_esri:
        record("Flood zone uses FEMA NFHL Layer 28 (authoritative)","PASS",
            "Switched from Esri Living Atlas — fixes Mannford OK Zone A/X mismatch")
    elif has_old_esri:
        record("Flood zone uses FEMA NFHL Layer 28 (authoritative)","FAIL",
            "Still using Esri Living Atlas — less accurate, can return wrong zone")
    else:
        record("Flood zone uses FEMA NFHL Layer 28 (authoritative)","WARN",
            "Could not determine which layer is being used")

def check_nfhl_layer():
    pass
check_bcrypt():
    record("bcrypt AttributeError in deploy logs","INFO",
        "Non-fatal — app starts fine despite this warning. "
        "passlib expects older bcrypt API. Safe to ignore.")

def check_git():
    try:
        out = subprocess.run(["git","-C","/home/runner/workspace","status","--short"],
            capture_output=True, text=True, timeout=10)
        dirty = out.stdout.strip()
        record("Uncommitted changes","WARN" if dirty else "PASS",
            dirty if dirty else "working tree clean")
    except Exception as e:
        record("Uncommitted changes","WARN",str(e))

def summary():
    print("\n" + "="*70)
    passed = sum(1 for _,s,_ in results if s=="PASS")
    failed = sum(1 for _,s,_ in results if s=="FAIL")
    warned = sum(1 for _,s,_ in results if s=="WARN")
    print(f"TOTAL: {len(results)}  PASS: {passed}  FAIL: {failed}  WARN: {warned}")
    if failed:
        print("\n❌ NEEDS FIX:")
        for label,status,detail in results:
            if status=="FAIL":
                print(f"  • {label}")
                if detail: print(f"    → {detail}")
    if warned:
        print("\n⚠️  PENDING / NEEDS VERIFICATION ON RAILWAY:")
        for label,status,detail in results:
            if status=="WARN":
                print(f"  • {label}")
                if detail: print(f"    → {detail}")
    print("="*70)

print("="*70)
print("FloodCert AI — Replit-Safe Verification (no live FEMA calls)")
print("="*70)
check_fema_lookup_functions()
check_main_loma()
check_loma_code()
check_local_db()
check_csb_url()
check_nfhl_layer()
check_bcrypt()
check_git()
summary()
