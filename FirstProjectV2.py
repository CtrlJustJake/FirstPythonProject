import pandas as pd
import numpy
import requests
### step 1. get scryfall ID ###
df = pd.read_csv("ManaBox_Collection.csv") 
df = df.rename(columns={'Scryfall ID' : 'ScryfallID'})
df = df[["ScryfallID"]]

### Step 2. batch scryfall ids into lists with 50 items ###
scryfall_list = df["ScryfallID"].tolist()
scryfall_batch = []

def ScryfallIdBatch(scryfall_list, chunk_size = 50):
    return [ scryfall_list[i: i + chunk_size] for i in range(0, len(scryfall_list), chunk_size)]
    
ScryfallIdBatch(scryfall_list)

scryfall_batch = ScryfallIdBatch(scryfall_list)

### Step 3. Send Batch over to Scryfall
pricing_data = []

headers = {
    'User-Agent': 'FirstProject/1.0',
    'Accept': 'application/json'
}

for batch in scryfall_batch:
    payload = {'identifiers': [{'id': card_id} for card_id in batch]}
    response = requests.post('https://api.scryfall.com/cards/collection', json=payload, headers=headers)

    if response.status_code == 200:
        cards = response.json().get('data', [])
        for card in cards:
            prices = card.get('prices', {})
            price = prices.get('usd')
            if price is None:
                price = prices.get('usd_foil')
            if price is None:
                price = prices.get('usd_etched')

            pricing_data.append({
                'Name': card.get('name'),
                'ScryfallID': card.get('id'),
                'CurrentPriceUSD': price
            })
    else:
        print("Error!!!", response.status_code)

df = pd.DataFrame(pricing_data)

df["CurrentPriceUSD"] = pd.to_numeric(df["CurrentPriceUSD"], errors="coerce")
cols = [c for c in ['CurrentPriceUSD', 'Purchase price', 'Name'] if c in df]
df = df[cols]

# Clean it back up
df = df.sort_values("CurrentPriceUSD", ascending=False)

df = df.head(100)

df.head(100)
