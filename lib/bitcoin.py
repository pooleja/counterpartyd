"""
Craft, sign and broadcast Bitcoin transactions.
Interface with Bitcoind.
"""

import os
import sys
import binascii
import json
import hashlib
import re
import time
import getpass
import decimal
import logging

import requests
from pycoin.ecdsa import generator_secp256k1, public_pair_for_secret_exponent
from pycoin.encoding import wif_to_tuple_of_secret_exponent_compressed, public_pair_to_sec
from pycoin.scripts import bitcoin_utils
from Crypto.Cipher import ARC4

from . import (config, exceptions)

# Constants
OP_RETURN = b'\x6a'
OP_PUSHDATA1 = b'\x4c'
OP_DUP = b'\x76'
OP_HASH160 = b'\xa9'
OP_EQUALVERIFY = b'\x88'
OP_CHECKSIG = b'\xac'
OP_1 = b'\x51'
OP_2 = b'\x52'
OP_CHECKMULTISIG = b'\xae'
b58_digits = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'

D = decimal.Decimal
dhash = lambda x: hashlib.sha256(hashlib.sha256(x).digest()).digest()
bitcoin_rpc_session = None


def get_block_count():
    return int(rpc('getblockcount', []))
    
def get_block_hash(block_index):
    return rpc('getblockhash', [block_index])

def is_mine (address):
    return rpc('validateaddress', [address])['ismine']

def send_raw_transaction (tx_hex):
    return rpc('sendrawtransaction', [tx_hex])

def get_raw_transaction (tx_hash):
    return rpc('getrawtransaction', [tx_hash, 1])

def get_block (block_hash):
    return rpc('getblock', [block_hash])

def get_block_hash (block_index):
    return rpc('getblockhash', [block_index])

def decode_raw_transaction (unsigned_tx_hex):
    return rpc('decoderawtransaction', [unsigned_tx_hex])

def get_wallet ():
    for group in rpc('listaddressgroupings', []):
        for bunch in group:
            yield bunch


def bitcoind_check (db):
    """Checks blocktime of last block to see if Bitcoind is running behind."""
    block_count = rpc('getblockcount', [])
    block_hash = rpc('getblockhash', [block_count])
    block = rpc('getblock', [block_hash])
    time_behind = time.time() - block['time']   # How reliable is the block time?!
    if time_behind > 60 * 60 * 2:   # Two hours.
        raise exceptions.BitcoindError('Bitcoind is running about {} seconds behind.'.format(round(time_behind)))

def connect (host, payload, headers):
    global bitcoin_rpc_session
    if not bitcoin_rpc_session: bitcoin_rpc_session = requests.Session()
    TRIES = 12
    for i in range(TRIES):
        try:
            response = bitcoin_rpc_session.post(host, data=json.dumps(payload), headers=headers)
            if i > 0: print('Successfully connected.', file=sys.stderr)
            return response
        except requests.exceptions.ConnectionError:
            print('Could not connect to Bitcoind. Sleeping for five seconds. (Try {}/{})'.format(i+1, TRIES), file=sys.stderr)
            time.sleep(5)
    return None

def wallet_unlock ():
    getinfo = rpc('getinfo', [])
    if 'unlocked_until' not in getinfo:
        return True
    elif getinfo['unlocked_until'] > 0:
        return True
    else:
        print('Wallet is locked.')
        passphrase = getpass.getpass('Enter your Bitcoind[‐Qt] wallet passhrase: ')
        print('Unlocking wallet for 60 seconds.')
        rpc('walletpassphrase', [passphrase, 60])

def rpc (method, params):
    headers = {'content-type': 'application/json'}
    payload = {
        "method": method,
        "params": params,
        "jsonrpc": "2.0",
        "id": 0,
    }

    '''
    if config.PREFIX == config.UNITTEST_PREFIX:
        CURR_DIR = os.path.dirname(os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__))))
        CURR_DIR += '/../test/'
        open(CURR_DIR + '/rpc.new', 'a') as f
        f.write(payload)
    '''

    response = connect(config.BITCOIND_RPC, payload, headers)
    if response == None:
        if config.TESTNET: network = 'testnet'
        else: network = 'mainnet'
        raise exceptions.BitcoindRPCError('Cannot communicate with Bitcoind. (counterpartyd is set to run on {}, is Bitcoind?)'.format(network))
    elif response.status_code not in (200, 500):
        raise exceptions.BitcoindRPCError(str(response.status_code) + ' ' + response.reason)

    '''
    if config.PREFIX == config.UNITTEST_PREFIX:
        print(response)
        f.close()
    '''

    # Return result, with error handling.
    response_json = response.json()
    if 'error' not in response_json.keys() or response_json['error'] == None:
        return response_json['result']
    elif response_json['error']['code'] == -5:   # RPC_INVALID_ADDRESS_OR_KEY
        raise exceptions.BitcoindError('{} Is txindex enabled in Bitcoind?'.format(response_json['error']))
    elif response_json['error']['code'] == -4:   # Unknown private key (locked wallet?)
        # If address in wallet, attempt to unlock.
        address = params[0]
        if rpc('validateaddress', [address])['ismine']:
            raise exceptions.BitcoindError('Wallet is locked.')
        else:   # When will this happen?
            raise exceptions.BitcoindError('Source address not in wallet.')
    # elif config.PREFIX == config.UNITTEST_PREFIX:
    #     print(method)
    else:
        raise exceptions.BitcoindError('{}'.format(response_json['error']))

def base58_check_encode(b, version):
    b = binascii.unhexlify(bytes(b, 'utf-8'))
    d = version + b   # mainnet

    address_hex = d + dhash(d)[:4]

    # Convert big‐endian bytes to integer
    n = int('0x0' + binascii.hexlify(address_hex).decode('utf8'), 16)

    # Divide that integer into base58
    res = []
    while n > 0:
        n, r = divmod (n, 58)
        res.append(b58_digits[r])
    res = ''.join(res[::-1])

    # Encode leading zeros as base58 zeros
    czero = 0
    pad = 0
    for c in d:
        if c == czero: pad += 1
        else: break
    return b58_digits[0] * pad + res

def base58_decode (s, version):
    # Convert the string to an integer
    n = 0
    for c in s:
        n *= 58
        if c not in b58_digits:
            raise exceptions.InvalidBase58Error('Not a valid base58 character:', c)
        digit = b58_digits.index(c)
        n += digit

    # Convert the integer to bytes
    h = '%x' % n
    if len(h) % 2:
        h = '0' + h
    res = binascii.unhexlify(h.encode('utf8'))

    # Add padding back.
    pad = 0
    for c in s[:-1]:
        if c == b58_digits[0]: pad += 1
        else: break
    k = version * pad + res

    addrbyte, data, chk0 = k[0:1], k[1:-4], k[-4:]
    if addrbyte != version:
        raise exceptions.VersionByteError('incorrect version byte')
    chk1 = dhash(addrbyte + data)[:4]
    if chk0 != chk1:
        raise exceptions.Base58ChecksumError('Checksum mismatch: %r ≠ %r' % (chk0, chk1))
    return data

def var_int (i):
    if i < 0xfd:
        return (i).to_bytes(1, byteorder='little')
    elif i <= 0xffff:
        return b'\xfd' + (i).to_bytes(2, byteorder='little')
    elif i <= 0xffffffff:
        return b'\xfe' + (i).to_bytes(4, byteorder='little')
    else:
        return b'\xff' + (i).to_bytes(8, byteorder='little')

def op_push (i):
    if i < 0x4c:
        return (i).to_bytes(1, byteorder='little')              # Push i bytes.
    elif i <= 0xff:
        return b'\x4c' + (i).to_bytes(1, byteorder='little')    # OP_PUSHDATA1
    elif i <= 0xffff:
        return b'\x4d' + (i).to_bytes(2, byteorder='little')    # OP_PUSHDATA2
    else:
        return b'\x4e' + (i).to_bytes(4, byteorder='little')    # OP_PUSHDATA4

def serialise (encoding, inputs, destination_outputs, data_output=None, change_output=None, source=None, pubkey=None):
    s  = (1).to_bytes(4, byteorder='little')                # Version

    # Number of inputs.
    s += var_int(int(len(inputs)))

    # List of Inputs.
    for i in range(len(inputs)):
        txin = inputs[i]
        s += binascii.unhexlify(bytes(txin['txid'], 'utf-8'))[::-1]         # TxOutHash
        s += txin['vout'].to_bytes(4, byteorder='little')   # TxOutIndex

        script = binascii.unhexlify(bytes(txin['scriptPubKey'], 'utf-8'))
        s += var_int(int(len(script)))                      # Script length
        s += script                                         # Script
        s += b'\xff' * 4                                    # Sequence

    # Number of outputs.
    n = 0
    n += len(destination_outputs)
    if data_output:
        data_array, value = data_output
        for data_chunk in data_array: n += 1
    else:
        data_array = []
    if change_output: n += 1
    s += var_int(n)

    # Destination output.
    for address, value in destination_outputs:
        pubkeyhash = base58_decode(address, config.ADDRESSVERSION)
        s += value.to_bytes(8, byteorder='little')          # Value
        script = OP_DUP                                     # OP_DUP
        script += OP_HASH160                                # OP_HASH160
        script += op_push(20)                               # Push 0x14 bytes
        script += pubkeyhash                                # pubKeyHash
        script += OP_EQUALVERIFY                            # OP_EQUALVERIFY
        script += OP_CHECKSIG                               # OP_CHECKSIG
        s += var_int(int(len(script)))                      # Script length
        s += script

    # Data output.
    for data_chunk in data_array:
        data_array, value = data_output # DUPE
        s += value.to_bytes(8, byteorder='little')        # Value

        # Get source public key (either provided as a string or derived from a private key in the wallet).
        if encoding in ('multisig', 'pubkeyhash'):
            if pubkey:
                pubkeypair = bitcoin_utils.parse_as_public_pair(pubkey)
                source_pubkey = public_pair_to_sec(pubkeypair, compressed=True)
            else:
                if config.PREFIX == config.UNITTEST_PREFIX:
                    private_key_wif = 'cPdUqd5EbBWsjcG9xiL1hz8bEyGFiz4SW99maU9JgpL9TEcxUf3j'
                else:
                    private_key_wif = rpc('dumpprivkey', [source])
                if private_key_wif[0] == 'c': testnet = True
                else: testnet = False
                secret_exponent, compressed = wif_to_tuple_of_secret_exponent_compressed(private_key_wif, is_test=testnet)
                public_pair = public_pair_for_secret_exponent(generator_secp256k1, secret_exponent)
                source_pubkey = public_pair_to_sec(public_pair, compressed=compressed)

        if encoding == 'multisig':
            # Get data (fake) public key.
            pad_length = 33 - 1 - len(data_chunk)
            assert pad_length >= 0
            data_pubkey = bytes([len(data_chunk)]) + data_chunk + (pad_length * b'\x00')
            # Construct script.
            script = OP_1                                   # OP_1
            script += op_push(len(source_pubkey))           # Push bytes of source public key
            script += source_pubkey                         # Source public key
            script += op_push(len(data_pubkey))             # Push bytes of data chunk (fake) public key
            script += data_pubkey                           # Data chunk (fake) public key
            script += OP_2                                  # OP_2
            script += OP_CHECKMULTISIG                      # OP_CHECKMULTISIG
        elif encoding == 'opreturn':
            script = OP_RETURN                              # OP_RETURN
            script += op_push(len(data_chunk))              # Push bytes of data chunk (NOTE: OP_SMALLDATA?)
            script += data_chunk                            # Data chunk
        elif encoding == 'pubkeyhash':
            pad_length = 20 - 1 - len(data_chunk)
            assert pad_length >= 0
            obj1 = ARC4.new(binascii.unhexlify(inputs[0]['txid']))  # Arbitrary, easy‐to‐find, unique key.
            pubkeyhash = bytes([len(data_chunk)]) + data_chunk + (pad_length * b'\x00')
            pubkeyhash_encrypted = obj1.encrypt(pubkeyhash)
            # Construct script.
            script = OP_DUP                                     # OP_DUP
            script += OP_HASH160                                # OP_HASH160
            script += op_push(20)                               # Push 0x14 bytes
            script += pubkeyhash_encrypted                      # pubKeyHash
            script += OP_EQUALVERIFY                            # OP_EQUALVERIFY
            script += OP_CHECKSIG                               # OP_CHECKSIG
        else:
            raise exceptions.TransactionError('Unknown encoding‐scheme.')

        s += var_int(int(len(script)))                      # Script length
        s += script

    # Change output.
    if change_output:
        address, value = change_output
        pubkeyhash = base58_decode(address, config.ADDRESSVERSION)
        s += value.to_bytes(8, byteorder='little')          # Value
        script = OP_DUP                                     # OP_DUP
        script += OP_HASH160                                # OP_HASH160
        script += op_push(20)                               # Push 0x14 bytes
        script += pubkeyhash                                # pubKeyHash
        script += OP_EQUALVERIFY                            # OP_EQUALVERIFY
        script += OP_CHECKSIG                               # OP_CHECKSIG
        s += var_int(int(len(script)))                      # Script length
        s += script

    s += (0).to_bytes(4, byteorder='little')                # LockTime
    return s

# Prefer outputs less than dust size, then bigger is better.
def input_value_weight(amount):
    if amount * config.UNIT <= config.REGULAR_DUST_SIZE:
        return 0
    else:
        return 1/amount

def get_inputs (source, total_btc_out, unittest=False):
    """List unspent inputs for source."""
    if not unittest:
        if rpc('validateaddress', [source])['ismine']:
            listunspent = rpc('listunspent', [])
        else:
            listunspent = get_unspent_txouts(source, normalize=True)
    else:
        CURR_DIR = os.path.dirname(os.path.realpath(os.path.join(os.getcwd(), os.path.expanduser(__file__))))
        with open(CURR_DIR + '/../test/listunspent.test.json', 'r') as listunspent_test_file:   # HACK
            listunspent = json.load(listunspent_test_file)
    unspent = [coin for coin in listunspent if coin['address'] == source]
    inputs, total_btc_in = [], 0
    change_quantity = 0
    for coin in sorted(unspent,key=lambda x:input_value_weight(x['amount'])):
        inputs.append(coin)
        total_btc_in += round(coin['amount'] * config.UNIT)
        change_quantity = total_btc_in - total_btc_out
        if total_btc_in >= total_btc_out and (change_quantity == 0 or change_quantity >= config.REGULAR_DUST_SIZE): # If change is necessary, must not be a dust output.
            return inputs, total_btc_in, change_quantity

    # Approximate needed change by with most recently calculated quantity.
    return None, None, change_quantity

# Replace unittest flag with fake bitcoind JSON-RPC server.
def transaction (tx_info, encoding, pubkey=None, unittest=False):
    source, destination_outputs, fee, data = tx_info
    if encoding not in ('pubkeyhash', 'multisig', 'opreturn'):
        raise exceptions.TransactionError('Unknown encoding‐scheme.')

    # Protocol change.
    if encoding == 'pubkeyhash' and get_block_count() < 293000 and not config.TESTNET:
        raise exceptions.TransactionError('pubkeyhash encoding unsupported before block 293000')
    
    if not isinstance(fee, int):
        raise exceptions.TransactionError('Fee must be in satoshis')

    if config.PREFIX == config.UNITTEST_PREFIX: unittest = True

    # Validate source and all destination addresses.
    destinations = [address for address, value in destination_outputs]
    for address in destinations + [source]:
        if address:
            try:
                base58_decode(address, config.ADDRESSVERSION)
            except Exception:   # TODO
                raise exceptions.InvalidAddressError('Invalid Bitcoin address:',
                                          address)

    # Check that the source is in wallet.
    if not unittest and encoding in ('multisig') and not pubkey:
        if not rpc('validateaddress', [source])['ismine']:
            raise exceptions.InvalidAddressError('Not one of your Bitcoin addresses:', source)

    # Check that the destination output isn't a dust output.
    # Set null values to dust size.
    new_destination_outputs = []
    for address, value in destination_outputs:
        if encoding == 'multisig':
            if value == None: value = config.MULTISIG_DUST_SIZE
            if not value >= config.MULTISIG_DUST_SIZE:
                raise exceptions.TransactionError('Destination output is below the dust target value.')
        else:
            if value == None: value = config.REGULAR_DUST_SIZE
            if not value >= config.REGULAR_DUST_SIZE:
                raise exceptions.TransactionError('Destination output is below the dust target value.')
        new_destination_outputs.append((address, value))
    destination_outputs = new_destination_outputs

    # Divide data into chunks.
    if data:
        def chunks(l, n):
            """ Yield successive n‐sized chunks from l.
            """
            for i in range(0, len(l), n): yield l[i:i+n]
        if encoding == 'pubkeyhash':
            data_array = list(chunks(data + config.PREFIX, 20 - 1)) # Prefix is also a suffix here.
        elif encoding == 'multisig':
            data_array = list(chunks(data, 33 - 1))
        elif encoding == 'opreturn':
            data_array = list(chunks(data, 80))
            assert len(data_array) == 1 # Only one OP_RETURN output currently supported (messages should all be shorter than 80 bytes, at the moment).
    else:
        data_array = []

    # Calculate total BTC to be sent.
    total_btc_out = fee
    if encoding == 'multisig': data_value = config.MULTISIG_DUST_SIZE
    elif encoding == 'opreturn': data_value = config.OP_RETURN_VALUE
    else: data_value = config.REGULAR_DUST_SIZE
    for data_chunk in data_array: total_btc_out += data_value
    total_btc_out += sum([value for address, value in destination_outputs])

    # Construct inputs.
    inputs, total_btc_in, change_quantity = get_inputs(source, total_btc_out, unittest=unittest)
    if not inputs:
        raise exceptions.BalanceError('Insufficient bitcoins at address {}. (Need {} BTC.)'.format(source, (total_btc_out + max(change_quantity, 0)) / config.UNIT))

    # Construct outputs.
    if data: data_output = (data_array, data_value)
    else: data_output = None
    if change_quantity: change_output = (source, change_quantity)
    else: change_output = None

    # Serialise inputs and outputs.
    transaction = serialise(encoding, inputs, destination_outputs, data_output, change_output, source=source, pubkey=pubkey)
    unsigned_tx_hex = binascii.hexlify(transaction).decode('utf-8')
    return unsigned_tx_hex

def transmit (unsigned_tx_hex):
    # Sign transaction.
    result = rpc('signrawtransaction', [unsigned_tx_hex])
    if result['complete']:
        signed_tx_hex = result['hex']
        return rpc('sendrawtransaction', [signed_tx_hex])

def normalize_quantity(quantity, divisible=True):
    if divisible:
        return float((D(quantity) / D(config.UNIT)).quantize(D('.00000000'), rounding=decimal.ROUND_HALF_EVEN)) 
    else: return quantity

def get_btc_balance(address, normalize=False):
    """returns the BTC balance for a specific address"""
    if config.INSIGHT_ENABLE:
        r = requests.get(config.INSIGHT + '/api/addr/' + address)
        if r.status_code != 200:
            return "???"
        else:
            data = r.json()
            return data['balance'] if normalize else data['balanceSat']
    else: #use blockchain
        r = requests.get("https://blockchain.info/q/addressbalance/" + address)
        # ^any other services that provide this?? (blockexplorer.com doesn't...)
        if r.status_code != 200:
            return "???"
        else:
            return normalize_quantity(int(r.text)) if normalize else int(r.text)

def get_btc_supply(normalize=False):
    """returns the total supply of BTC (based on what bitcoind says the current block height is)"""
    block_count = get_block_count()
    blocks_remaining = block_count
    total_supply = 0 
    reward = 50.0
    while blocks_remaining > 0:
        if blocks_remaining >= 210000:
            blocks_remaining -= 210000
            total_supply += 210000 * reward
            reward /= 2
        else:
            total_supply += (blocks_remaining * reward)
            blocks_remaining = 0
    return total_supply if normalize else int(total_supply * config.UNIT)

def get_unspent_txouts(address, normalize=False):
    """returns a list of unspent outputs for a specific address
    @return: A list of dicts, with each entry in the dict having the following keys:
        * 
    """
    if config.INSIGHT_ENABLE:
        r = requests.get(config.INSIGHT + '/api/addr/' + address + '/utxo')
        if r.status_code != 200:
            raise Exception("Can't get unspent txouts: insight returned bad status code: %s" % r.status_code)

        data = r.json()
        if not normalize: #listed normalized by default out of insight...we need to take to satoshi
            for d in data:
                d['quantity'] = int(d['quantity'] * config.UNIT)
        return data
    else: #use blockchain
        r = requests.get("https://blockchain.info/unspent?active=" + address)
        if r.status_code == 500 and r.text.lower() == "no free outputs to spend":
            return []
        elif r.status_code != 200:
            raise Exception("Bad status code returned from blockchain.info: %s" % r.status_code)
        data = r.json()['unspent_outputs']
        results = []
        for d in data:
            #blockchain.info lists the txhash in some weird reversed string notation with character pairs fipped...fun
            d['tx_hash'] = d['tx_hash'][::-1] #reverse string
            d['tx_hash'] = ''.join([d['tx_hash'][i:i+2][::-1] for i in range(0, len(d['tx_hash']), 2)]) #flip the character pairs within the string
            results.append({
                'account': "",
                'address': address,
                'txid': d['tx_hash'],
                'vout': d['tx_output_n'],
                'ts': None,
                'scriptPubKey': d['script'],
                'quantity': normalize_quantity(d['value']) if normalize else d['value'],
                'confirmations': d['confirmations'],
            })
        return results

# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
