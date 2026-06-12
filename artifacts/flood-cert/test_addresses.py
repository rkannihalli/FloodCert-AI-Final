import sys,os
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from geocoder import geocode_address
from fema_lookup import lookup_fema_data
G=chr(27)+'[92m';R=chr(27)+'[91m';Y=chr(27)+'[93m';B=chr(27)+'[1m';E=chr(27)+'[0m'
TESTS=[
('879 Willow Ridge Rd, Hudson, WI 54016',None),
('106 W 6th South St, Summerville, SC 29483',None),
('1053 Brewer Ridge Rd, Bulverde, TX 78163','X'),
('301 Satinwood Drive Lot 1, Santa Rosa Beach, FL 32459','X'),
('7710 Lavender Ct, Katy, TX 77493',None),
('11 Brabrook Rd, Acton, MA 01720',None),
('911 Lake St, Dalton, GA 30721',None),
('2413 Denmead St, Lakewood, CA 90712',None),
('9192 Horton Hwy, College Grove, TN 37046',None),
('329 Willard St, Jamestown, NY 14701',None),
('70 Circle Dr, Wallingford, CT 06492',None),
('2909 Travis St, Peru, IN 46970',None),
('4248 Duck Dr, Ann Arbor, MI 48103',None),
('4250 Duck Dr, Ann Arbor, MI 48103',None),
]
passed=failed=warns=0
print(B+'FloodCert Regression Test'+E)
print('='*55)
for i,(addr,exp) in enumerate(TESTS,1):
    print(str(i)+'/'+str(len(TESTS))+' '+addr)
    geo=geocode_address(addr)
    if not geo:
        print('  '+R+'GEOCODE FAILED'+E)
        failed+=1
        continue
    print('  via '+geo.source.upper()+' score='+str(int(geo.score))+' '+geo.matched_address)
    fema=lookup_fema_data(geo.lat,geo.lon)
    print('  Zone='+str(fema.flood_zone)+' Panel='+str(fema.firm_panel_number)+' CID='+str(fema.nfip_community_number))
    for conf in fema.data_conflicts:
        print('  '+Y+'CONFLICT: '+conf+E)
        warns+=1
    if exp:
        actual=(fema.flood_zone or '').split()[0].upper()
        if actual==exp.upper():
            print('  '+G+'OK: expected '+exp+' got '+str(fema.flood_zone)+E)
            passed+=1
        else:
            print('  '+R+'MISMATCH: expected '+exp+' got '+str(fema.flood_zone)+E)
            failed+=1
    else:
        passed+=1
print('='*55)
print(B+G+str(passed)+' passed'+E+'  '+R+str(failed)+' failed'+E+'  '+Y+str(warns)+' conflicts'+E)
sys.exit(1 if failed else 0)
