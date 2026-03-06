#!/usr/bin/env python3
import os, sys
from dotenv import load_dotenv
load_dotenv()

private_key = os.environ.get('PRIVATE_KEY', '').strip()
if not private_key:
    print('ERROR: PRIVATE_KEY is empty in .env')
    sys.exit(1)

if not private_key.startswith('0x'):
    private_key = '0x' + private_key

print(f'Deriving Polymarket CLOB API credentials...')
print(f'Using key: {private_key[:6]}...{private_key[-4:]}')

try:
    from py_clob_client.client import ClobClient
    client = ClobClient(
        host='https://clob.polymarket.com',
        chain_id=137,
        key=private_key,
    )
    creds = client.create_or_derive_api_creds()

    # ApiCreds is an object with attributes, not a dict
    api_key = getattr(creds, 'api_key', '') or getattr(creds, 'apiKey', '')
    api_secret = getattr(creds, 'secret', '') or getattr(creds, 'api_secret', '')
    api_passphrase = getattr(creds, 'passphrase', '')

    # If still empty, try converting to dict
    if not api_key:
        d = vars(creds) if hasattr(creds, '__dict__') else {}
        print(f'DEBUG creds type: {type(creds)}')
        print(f'DEBUG creds attrs: {dir(creds)}')
        if hasattr(creds, '__dict__'):
            print(f'DEBUG creds dict: {vars(creds)}')
        sys.exit(1)

    print()
    print('SUCCESS! Copy these into your .env:')
    print('=' * 60)
    print(f'POLYMARKET_API_KEY={api_key}')
    print(f'POLYMARKET_API_SECRET={api_secret}')
    print(f'POLYMARKET_API_PASSPHRASE={api_passphrase}')
    print('=' * 60)

except Exception as e:
    print(f'Error: {e}')
    import traceback
    traceback.print_exc()
    sys.exit(1)
