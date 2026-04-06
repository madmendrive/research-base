import json

with open('config/companies.json', encoding='utf-8') as f:
    companies = json.load(f)

new = {
    '1303 TT': {'name': 'Nan Ya Plastics Corp', 'market': 'TW', 'ir_url': 'https://www.npc.com.tw/j2npc/enus/investor', 'twse_code': '1303', 'fiscal_year_end': 'December'},
    '1590 TT': {'name': 'AirTAC International Group', 'market': 'TW', 'ir_url': 'https://www.airtac.com/en/investor.html', 'twse_code': '1590', 'fiscal_year_end': 'December'},
    '2308 TT': {'name': 'Delta Electronics Inc', 'market': 'TW', 'ir_url': 'https://www.deltaww.com/en-US/ir', 'twse_code': '2308', 'fiscal_year_end': 'December'},
    '2317 TT': {'name': 'Hon Hai Precision Industry', 'market': 'TW', 'ir_url': 'https://www.honhai.com/en-us/investor-relations', 'twse_code': '2317', 'fiscal_year_end': 'December'},
    '2324 TT': {'name': 'Compal Electronics Inc', 'market': 'TW', 'ir_url': 'https://www.compal.com/investor-relations', 'twse_code': '2324', 'fiscal_year_end': 'December'},
    '2337 TT': {'name': 'Macronix International', 'market': 'TW', 'ir_url': 'https://www.macronix.com/en-us/ir', 'twse_code': '2337', 'fiscal_year_end': 'December'},
    '2345 TT': {'name': 'Accton Technology Corp', 'market': 'TW', 'ir_url': 'https://www.accton.com/investor/', 'twse_code': '2345', 'fiscal_year_end': 'December'},
    '2353 TT': {'name': 'Acer Inc', 'market': 'TW', 'ir_url': 'https://www.acer.com/corporate/en/investor-relations', 'twse_code': '2353', 'fiscal_year_end': 'December'},
    '2357 TT': {'name': 'ASUSTeK Computer Inc', 'market': 'TW', 'ir_url': 'https://www.asus.com/event/investor/', 'twse_code': '2357', 'fiscal_year_end': 'December'},
    '2360 TT': {'name': 'Chroma ATE Inc', 'market': 'TW', 'ir_url': 'https://www.chroma.com.tw/investor/', 'twse_code': '2360', 'fiscal_year_end': 'December'},
    '2395 TT': {'name': 'Advantech Co Ltd', 'market': 'TW', 'ir_url': 'https://www.advantech.com/en/investor-relations', 'twse_code': '2395', 'fiscal_year_end': 'December'},
    '2408 TT': {'name': 'Nanya Technology Corp', 'market': 'TW', 'ir_url': 'https://www.nanya.com/en/investor', 'twse_code': '2408', 'fiscal_year_end': 'December'},
    '2409 TT': {'name': 'AUO Corp', 'market': 'TW', 'ir_url': 'https://www.auo.com/en-global/Investor_Relations', 'twse_code': '2409', 'fiscal_year_end': 'December'},
    '2454 TT': {'name': 'MediaTek Inc', 'market': 'TW', 'ir_url': 'https://corp.mediatek.com/investor-relations', 'twse_code': '2454', 'fiscal_year_end': 'December'},
    '2474 TT': {'name': 'Catcher Technology Co Ltd', 'market': 'TW', 'ir_url': 'https://www.catcher.com.tw/en/investor/', 'twse_code': '2474', 'fiscal_year_end': 'December'},
    '3006 TT': {'name': 'Elite Semiconductor Memory', 'market': 'TW', 'ir_url': 'https://www.esmt.com.tw/en/investor', 'twse_code': '3006', 'fiscal_year_end': 'December'},
    '3008 TT': {'name': 'Largan Precision Co Ltd', 'market': 'TW', 'ir_url': 'https://www.largan.com.tw/html/investor/investor.php', 'twse_code': '3008', 'fiscal_year_end': 'December'},
    '3131 TT': {'name': 'Grand Process Technology', 'market': 'TW', 'ir_url': 'https://www.gpt.com.tw/en/investor/', 'twse_code': '3131', 'fiscal_year_end': 'December'},
    '3163 TT': {'name': 'Browave Corp', 'market': 'TW', 'ir_url': 'https://www.browave.com/investor/', 'twse_code': '3163', 'fiscal_year_end': 'December'},
    '3189 TT': {'name': 'Kinsus Interconnect Technology', 'market': 'TW', 'ir_url': 'https://www.kinsus.com.tw/en/investor/', 'twse_code': '3189', 'fiscal_year_end': 'December'},
    '3406 TT': {'name': 'Genius Electronic Optical', 'market': 'TW', 'ir_url': 'https://www.gseo.com/en/investor/', 'twse_code': '3406', 'fiscal_year_end': 'December'},
    '3481 TT': {'name': 'Innolux Corp', 'market': 'TW', 'ir_url': 'https://www.innolux.com/investor/', 'twse_code': '3481', 'fiscal_year_end': 'December'},
    '3661 TT': {'name': 'Alchip Technologies Ltd', 'market': 'TW', 'ir_url': 'https://www.alchip.com/investor/', 'twse_code': '3661', 'fiscal_year_end': 'December'},
    '3665 TT': {'name': 'BizLink Holding Inc', 'market': 'TW', 'ir_url': 'https://www.bizlinktech.com/investor-relations', 'twse_code': '3665', 'fiscal_year_end': 'December'},
    '3711 TT': {'name': 'ASE Technology Holding', 'market': 'TW', 'ir_url': 'https://www.aseglobal.com/en/InvestorRelations', 'twse_code': '3711', 'fiscal_year_end': 'December'},
    '4979 TT': {'name': 'LuxNet Corp', 'market': 'TW', 'ir_url': 'https://www.luxnet.com.tw/investor/', 'twse_code': '4979', 'fiscal_year_end': 'December'},
    '5269 TT': {'name': 'ASMedia Technology Inc', 'market': 'TW', 'ir_url': 'https://www.asmedia.com.tw/investor/', 'twse_code': '5269', 'fiscal_year_end': 'December'},
    '5274 TT': {'name': 'ASPEED Technology Inc', 'market': 'TW', 'ir_url': 'https://www.aspeedtech.com/investor/', 'twse_code': '5274', 'fiscal_year_end': 'December'},
    '6187 TT': {'name': 'All Ring Tech Co Ltd', 'market': 'TW', 'ir_url': 'https://www.allring.com.tw/en/investor/', 'twse_code': '6187', 'fiscal_year_end': 'December'},
    '6223 TT': {'name': 'MPI Corp', 'market': 'TW', 'ir_url': 'https://www.mpi.com.tw/en/investor/', 'twse_code': '6223', 'fiscal_year_end': 'December'},
    '6442 TT': {'name': 'EZconn Corp', 'market': 'TW', 'ir_url': 'https://www.ezconn.com/investor/', 'twse_code': '6442', 'fiscal_year_end': 'December'},
    '6488 TT': {'name': 'GlobalWafers Co Ltd', 'market': 'TW', 'ir_url': 'https://www.gw-semi.com/en/investor/', 'twse_code': '6488', 'fiscal_year_end': 'December'},
    '6510 TT': {'name': 'Chunghwa Precision Test Tech', 'market': 'TW', 'ir_url': 'https://www.chpt.com.tw/en/investor/', 'twse_code': '6510', 'fiscal_year_end': 'December'},
    '7769 TT': {'name': 'Hon Precision Inc', 'market': 'TW', 'ir_url': 'https://www.honprecision.com/investor/', 'twse_code': '7769', 'fiscal_year_end': 'December'},
    '8046 TT': {'name': 'Nan Ya PCB Corp', 'market': 'TW', 'ir_url': 'https://www.nanyapcb.com.tw/en/investor/', 'twse_code': '8046', 'fiscal_year_end': 'December'},
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
