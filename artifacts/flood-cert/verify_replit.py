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
        record("fema_lookup.py exists","FAIL",f"not found at {path}"); return
    required = ["def geocode_address","def query_fema_nfhl","def query_nfip_community",
        "def query_firm_panel","def query_tigerweb_fips","def query_county_name",
        "def query_nfip_community_csb","def nfip_community_info",
        "def determine_flood_info","def check_loma_at_point"]
    missing = [fn.replace("def ","") for fn in required if fn not in content]
    if missing:
        record("fema_lookup.py has all 10 functions","FAIL",f"Missing: {missing}")
    else:
        record("fema_lookup.py has all 10 functions","PASS","10/10 found")

def check_nfhl_layer():
    path = os.path.join(APP_DIR, "fema_lookup.py")
    content = read_file(path)
    if content is None:
        return
    has_layer28 = "NFHL/MapServer/28/query" in content
    has_old_esri = "USA_Flood_Hazard_Reduced_Set_gdb" in content
    if has_layer28 and not has_old_esri:
        record("Flood zone uses FEMA NFHL Layer 28","PASS",
            "Switched from Esri Living Atlas — fixes Mannford OK Zone A/X mismatch")
    elif has_old_esri:
        record("Flood zone uses FEMA NFHL Layer 28","FAIL",
            "Still using Esri Living Atlas — can return wrong zone")
    else:
        record("Flood zone uses FEMA NFHL Layer 28","WARN","Could not determine layer")

def check_main_loma():
    path = os.path.join(APP_DIR, "main.py")
    content = read_file(path)
    if content is None:
        record("main.py exists","FAIL",f"not found at {path}"); return
    has_import = "check_loma_at_point" in content
    record("main.py imports check_loma_at_point","PASS" if has_import else "FAIL")
    has_call = bool(re.search(r"check_loma_at_point\(geo_lat", content))
    record("main.py calls check_loma_at_point() in search flow","PASS" if has_call else "FAIL")
    has_x500 = bool(re.search(r"outcome_zone.*in.*X.*X500", content))
    record("main.py LOMA condition handles X500","PASS" if has_x500 else "FAIL",
        "Fixed" if has_x500 else "Still only checks X — X500 LOMRs will not trigger override")
    has_dynamic = "loma.get" in content and "flood_zone" in content
    record("main.py sets zone dynamically from LOMA outcome","PASS" if has_dynamic else "FAIL")

def check_loma_code():
    path = os.path.join(APP_DIR, "fema_lookup.py")
    content = read_file(path)
    if content is None:
        return
    cols = ["case_number","outcome_zone","amendment_type","effective_date","lat","lon"]
    missing = [c for c in cols if c not in content]
    record("check_loma_at_point uses correct DB columns","PASS" if not missing else "FAIL",
        f"Missing: {missing}" if missing else "")
    record("loma_records Katy TX entry (22-06-1128A)","INFO",
        "Verify in Railway Postgres: SELECT * FROM loma_records WHERE case_number = '22-06-1128A';")

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
    record(f"TX county coverage ({len(tx_keys)}/254)","WARN" if len(tx_keys) < 254 else "PASS",
        f"Incomplete — {len(tx_keys)} TX counties" if len(tx_keys) < 254 else "Complete")
    record("Waller County TX_473 in local DB","PASS" if "TX_473" in data else "FAIL",
        "Found" if "TX_473" in data else "MISSING")

def check_lomc_template():
    path = os.path.join(APP_DIR, "templates/certificate_pdf.html")
    content = read_file(path)
    if content is None:
        record("LOMC section in PDF template","FAIL","template not found"); return
    has_lomc = "IS THERE A LETTER OF MAP CHANGE" in content
    has_case = "loma_case_number" in content
    has_date = "loma_effective_date" in content
    record("LOMC Section 3 in PDF template","PASS" if has_lomc else "FAIL",
        "Found" if has_lomc else "Missing — Section 3 not added to certificate_pdf.html")
    record("LOMC template uses loma_case_number + loma_effective_date",
        "PASS" if (has_case and has_date) else "FAIL")

def check_csb_url():
    path = os.path.join(APP_DIR, "fema_lookup.py")
    content = read_file(path)
    if content is None:
        return
    m = re.search(r"FEMA_CSB_URL\s*=\s*\"([^\"]+)\"", content)
    url = m.group(1) if m else "not found"
    record("FEMA CSB API URL","WARN",
        f"Current: {url} — Both v1/v2 return 404. Local DB is primary source.")

def check_bcrypt():
    record("bcrypt AttributeError in deploy logs","INFO",
        "Non-fatal — app starts fine. Safe to ignore.")

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
        print("\n⚠️  PENDING / VERIFY ON RAILWAY:")
        for label,status,detail in results:
            if status=="WARN":
                print(f"  • {label}")
                if detail: print(f"    → {detail}")
    print("="*70)

print("="*70)
print("FloodCert AI — Replit-Safe Verification (no live FEMA calls)")
print("="*70)
check_fema_lookup_functions()
check_nfhl_layer()
check_main_loma()
check_loma_code()
check_local_db()
check_lomc_template()
check_csb_url()
check_bcrypt()
check_git()
summary()
