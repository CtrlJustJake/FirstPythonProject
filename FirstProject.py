import pandas as pd
import numpy
import requests

df = pd.read_csv("ManaBox_Collection.csv")
df = df.sort_values(["Purchase price"], ascending= False)
df = df[df["Purchase price"]>= 1.00]
df = df.head(50)
df = df.reset_index(drop=True)
df = df.rename(columns={'Scryfall ID' : 'ScryfallID'})

print(df.at[7,'ScryfallID'])
list_ids = df['ScryfallID'].dropna().tolist()

payload = {'identifiers': [{'id': card_id} for card_id in list_ids]}

headers = {'User-Agent': 'FirstProject/1.0', 'Accept': 'application/json'}

response = requests.post('https://api.scryfall.com/cards/collection', json=payload, headers=headers)

pricing_data = []

if response.status_code == 200:
    cards = response.json().get('data',[])
    for card in cards:
        prices = card.get('prices', {})
        price = prices.get('usd', {})
        if price is None:
            price = prices.get('usd_foil')
        if price is None:
            price = prices.get('usd_etched')
            
        pricing_data.append({
            'ScryfallID': card.get('id'),
            'CurrentPriceUSD': price
            })
else:
    print("Error!!!")

df_prices = pd.DataFrame(pricing_data)

df_prices['CurrentPriceUSD'] = pd.to_numeric(df_prices['CurrentPriceUSD'], errors='coerce')

df = df.merge(df_prices, on='ScryfallID', how='left')

df = df[['CurrentPriceUSD', 'Purchase price', 'Name',]]

df["GainLoss"] = df['CurrentPriceUSD'] - df['Purchase price']

df['PercentageDelta'] = df['CurrentPriceUSD'] / df['Purchase price'] - 1

df = df[['CurrentPriceUSD', 'Purchase price', 'GainLoss', 'PercentageDelta', 'Name']]