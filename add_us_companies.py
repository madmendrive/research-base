import json

with open('config/companies.json', encoding='utf-8') as f:
    companies = json.load(f)

new = {
    'ADEA': {'name': 'Adeia Inc', 'market': 'US', 'ir_url': 'https://ir.adeia.com', 'sec_cik': '0001803696', 'fiscal_year_end': 'December'},
    'AMAT': {'name': 'Applied Materials Inc', 'market': 'US', 'ir_url': 'https://ir.appliedmaterials.com', 'sec_cik': '0000006951', 'fiscal_year_end': 'October'},
    'AMZN': {'name': 'Amazon.com Inc', 'market': 'US', 'ir_url': 'https://ir.aboutamazon.com', 'sec_cik': '0001018724', 'fiscal_year_end': 'December'},
    'ANET': {'name': 'Arista Networks Inc', 'market': 'US', 'ir_url': 'https://investors.arista.com', 'sec_cik': '0001596532', 'fiscal_year_end': 'December'},
    'ARM': {'name': 'ARM Holdings PLC', 'market': 'US', 'ir_url': 'https://investors.arm.com', 'sec_cik': '0001973239', 'fiscal_year_end': 'March'},
    'AVGO': {'name': 'Broadcom Inc', 'market': 'US', 'ir_url': 'https://investors.broadcom.com', 'sec_cik': '0001730168', 'fiscal_year_end': 'November'},
    'CIEN': {'name': 'Ciena Corp', 'market': 'US', 'ir_url': 'https://investor.ciena.com', 'sec_cik': '0000936395', 'fiscal_year_end': 'October'},
    'COHR': {'name': 'Coherent Corp', 'market': 'US', 'ir_url': 'https://ir.coherent.com', 'sec_cik': '0000820318', 'fiscal_year_end': 'June'},
    'CRDO': {'name': 'Credo Technology Group', 'market': 'US', 'ir_url': 'https://investor.credosemi.com', 'sec_cik': '0001807794', 'fiscal_year_end': 'April'},
    'DELL': {'name': 'Dell Technologies Inc', 'market': 'US', 'ir_url': 'https://investors.delltechnologies.com', 'sec_cik': '0001571996', 'fiscal_year_end': 'January'},
    'FORM': {'name': 'FormFactor Inc', 'market': 'US', 'ir_url': 'https://ir.formfactor.com', 'sec_cik': '0001039399', 'fiscal_year_end': 'December'},
    'GFS': {'name': 'GlobalFoundries Inc', 'market': 'US', 'ir_url': 'https://investors.gf.com', 'sec_cik': '0001709048', 'fiscal_year_end': 'December'},
    'GLW': {'name': 'Corning Inc', 'market': 'US', 'ir_url': 'https://investor.corning.com', 'sec_cik': '0000024741', 'fiscal_year_end': 'December'},
    'HPE': {'name': 'Hewlett Packard Enterprise', 'market': 'US', 'ir_url': 'https://investors.hpe.com', 'sec_cik': '0001645590', 'fiscal_year_end': 'October'},
    'HPQ': {'name': 'HP Inc', 'market': 'US', 'ir_url': 'https://investor.hp.com', 'sec_cik': '0000047217', 'fiscal_year_end': 'October'},
    'IBM': {'name': 'International Business Machines', 'market': 'US', 'ir_url': 'https://www.ibm.com/investor', 'sec_cik': '0000051143', 'fiscal_year_end': 'December'},
    'INTC': {'name': 'Intel Corp', 'market': 'US', 'ir_url': 'https://www.intc.com/investor-relations', 'sec_cik': '0000050863', 'fiscal_year_end': 'December'},
    'LRCX': {'name': 'Lam Research Corp', 'market': 'US', 'ir_url': 'https://investor.lamresearch.com', 'sec_cik': '0000707549', 'fiscal_year_end': 'June'},
    'MU': {'name': 'Micron Technology Inc', 'market': 'US', 'ir_url': 'https://investors.micron.com', 'sec_cik': '0000723125', 'fiscal_year_end': 'September'},
    'NVDA': {'name': 'NVIDIA Corp', 'market': 'US', 'ir_url': 'https://investor.nvidia.com', 'sec_cik': '0001045810', 'fiscal_year_end': 'January'},
    'ORCL': {'name': 'Oracle Corp', 'market': 'US', 'ir_url': 'https://investor.oracle.com', 'sec_cik': '0001341439', 'fiscal_year_end': 'May'},
    'SMCI': {'name': 'Super Micro Computer Inc', 'market': 'US', 'ir_url': 'https://ir.supermicro.com', 'sec_cik': '0001375365', 'fiscal_year_end': 'June'},
    'SNPS': {'name': 'Synopsys Inc', 'market': 'US', 'ir_url': 'https://investor.synopsys.com', 'sec_cik': '0000883241', 'fiscal_year_end': 'October'},
    'STX': {'name': 'Seagate Technology Holdings', 'market': 'US', 'ir_url': 'https://investors.seagate.com', 'sec_cik': '0001137789', 'fiscal_year_end': 'June'},
    'SWKS': {'name': 'Skyworks Solutions Inc', 'market': 'US', 'ir_url': 'https://investors.skyworksinc.com', 'sec_cik': '0000004127', 'fiscal_year_end': 'October'},
    'TER': {'name': 'Teradyne Inc', 'market': 'US', 'ir_url': 'https://investors.teradyne.com', 'sec_cik': '0000097210', 'fiscal_year_end': 'December'},
    'TSLA': {'name': 'Tesla Inc', 'market': 'US', 'ir_url': 'https://ir.tesla.com', 'sec_cik': '0001318605', 'fiscal_year_end': 'December'},
    'VRT': {'name': 'Vertiv Holdings Co', 'market': 'US', 'ir_url': 'https://investors.vertiv.com', 'sec_cik': '0001674101', 'fiscal_year_end': 'December'},
    'WDC': {'name': 'Western Digital Corp', 'market': 'US', 'ir_url': 'https://investor.wdc.com', 'sec_cik': '0000106040', 'fiscal_year_end': 'July'},
}

existing = [t for t in new if t in companies]
if existing:
    print(f'Already exist (skipping): {existing}')
    for t in existing:
        del new[t]

companies.update(new)
with open('config/companies.json', 'w', encoding='utf-8') as f:
    json.dump(companies, f, indent=2, ensure_ascii=False)
    f.write('\n')

print(f'Added {len(new)} companies. Total: {len(companies)}')
for t in sorted(new.keys()):
    print(f'  {t}: {new[t]["name"]} (CIK: {new[t]["sec_cik"]}, FY: {new[t]["fiscal_year_end"]})')
