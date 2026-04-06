import json

with open('config/companies.json', encoding='utf-8') as f:
    companies = json.load(f)

updates = {
    '1303 TT': {
        'ir_url': 'https://www.npc.com.tw/j2npc/enus/investor/Annual%20Reports',
    },
    '1590 TT': {
        'ir_url': 'https://us-en.airtac.com/tzr.aspx?c_kind=5',
    },
    '2308 TT': {
        'ir_url': 'https://www.deltaww.com/en-US/investors/annual-Reports',
        'ir_extra_urls': ['https://www.deltaww.com/ir/financialReport.aspx?Year=&cateID=3&hl=en-US&pid=3&secID=4&tid=0'],
    },
    '2337 TT': {
        'ir_url': 'https://www.mxic.com.tw/en-us/about/investor-relations/Pages/financial-reports.aspx',
        'ir_extra_urls': ['https://www.mxic.com.tw/en-us/about/investor-relations/Pages/quarterly-results.aspx'],
    },
    '2353 TT': {
        'ir_url': 'https://www.acer.com/corporate/en/investor-relations/financials/annual-reports',
        'ir_extra_urls': ['https://www.acer.com/corporate/en/investor-relations/financials/quarterly-reports'],
    },
    '2360 TT': {
        'ir_url': 'https://www.chromaate.com/en/investors/quarterly_results',
        'ir_extra_urls': ['https://www.chromaate.com/en/investors/financial_reports'],
    },
    '2395 TT': {
        'ir_url': 'https://www.advantech.com/en-us/investor/annual-report',
        'ir_extra_urls': ['https://www.advantech.com/en-us/investor/quarterly-results'],
    },
    '2408 TT': {
        'ir_url': 'https://www.nanya.com/en/IR/42',
        'ir_extra_urls': ['https://www.nanya.com/en/IR/39'],
    },
    '2409 TT': {
        'ir_url': 'https://www.auo.com/en-global/financial_results/index',
        'ir_extra_urls': ['https://www.auo.com/en-global/investor_conference/index'],
    },
    '2474 TT': {
        'ir_url': 'https://www.catcher-group.com/investor_financial.aspx',
        'ir_extra_urls': ['https://www.catcher-group.com/investor_financial_fs.aspx'],
    },
    '3006 TT': {
        'ir_url': 'https://www.esmt.com.tw/en/Investor/Financial-Information',
    },
    '3008 TT': {
        'ir_url': 'http://www.largan.com.tw/en/investor/finance',
    },
    '3131 TT': {
        'ir_url': 'https://www.gptc.com.tw/en/investors/Investros/',
        'ir_extra_urls': ['https://www.gptc.com.tw/en/investors/Stock-Information/'],
    },
    '3163 TT': {
        'ir_url': 'https://www.browave.com/Investors/',
        'ir_extra_urls': ['https://www.browave.com/investors/Category/45'],
    },
    '3189 TT': {
        'ir_url': 'https://www.kinsus.com.tw/en-global/Html/investor-services',
        'ir_extra_urls': ['https://www.kinsus.com.tw/en-global/Download/financial-information'],
    },
    '3406 TT': {
        'ir_url': 'https://www.gseo.com/investor-relations',
    },
    '3481 TT': {
        'ir_url': 'https://www.innolux.com/en/ir/financials/annual_reports.html',
    },
    '3665 TT': {
        'ir_url': 'https://www.bizlinktech.com/financial-information',
    },
    '4979 TT': {
        'ir_url': 'https://www.luxnetcorp.com.tw/en-US/investor-information',
        'ir_extra_urls': ['https://www.luxnetcorp.com.tw/en-US/investor-information-140'],
    },
    '5269 TT': {
        'ir_url': 'https://www.asmedia.com.tw/investors02-3.html',
        'ir_extra_urls': [
            'https://www.asmedia.com.tw/investors02.html',
            'https://www.asmedia.com.tw/investors02-2.html',
            'https://www.asmedia.com.tw/investors02-4.html',
        ],
    },
    '6187 TT': {
        'ir_url': 'https://allring-tech.com.tw/index.php?id=132&index=2&lang=en&option=module&task=showlist',
    },
    '6442 TT': {
        'ir_url': 'https://www.ezconn.com/en/investor/financial?financial-tab=monthly',
    },
    '6488 TT': {
        'ir_url': 'https://www.sas-globalwafers.com/en/investor/',
        'ir_extra_urls': ['https://www.sas-globalwafers.com/en/investor/financial-information_en/'],
    },
    '6510 TT': {
        'ir_url': 'https://www.cht-pt.com.tw/eng/xccompdoc?xsmsid=0H066574157047621001',
        'ir_extra_urls': [
            'https://www.cht-pt.com.tw/eng/xccompdoc?xsmsid=0L265368024209225653',
            'https://www.cht-pt.com.tw/eng/xccompdoc?xsmsid=0H107601516710605896',
        ],
    },
    '7769 TT': {
        'ir_url': 'https://www.honprec.com/en/finance/',
        'ir_extra_urls': ['https://www.honprec.com/en/stock/'],
    },
    '8046 TT': {
        'ir_url': 'https://www.nanyapcb.com.tw/nypcb/english/InvestorRelations/Shareholders/StockQuotes.aspx',
    },
}

for ticker, new_data in updates.items():
    if ticker in companies:
        companies[ticker].update(new_data)
        print(f'  Updated: {ticker}')
    else:
        print(f'  NOT FOUND: {ticker}')

with open('config/companies.json', 'w', encoding='utf-8') as f:
    json.dump(companies, f, indent=2, ensure_ascii=False)
    f.write('\n')

print(f'\nUpdated {len(updates)} companies')
