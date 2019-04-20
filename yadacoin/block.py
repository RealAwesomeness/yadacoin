import json
import hashlib
import base64
import time

from decimal import Decimal, getcontext
from bitcoin.signmessage import BitcoinMessage, VerifyMessage
from bitcoin.wallet import P2PKHBitcoinAddress
from coincurve.utils import verify_signature
from logging import getLogger

from yadacoin.chain import CHAIN
from yadacoin.config import get_config
from yadacoin.fastgraph import FastGraph
from yadacoin.transaction import TransactionFactory, Transaction, InvalidTransactionException, ExternalInput


def quantize_eight(value):
    value = Decimal(value)
    value = value.quantize(Decimal('0.00000000'))
    return value


class BlockFactory(object):
    def __init__(self, transactions, public_key, private_key, force_version=None, index=None, force_time=None):
        try:
            self.config = get_config()
            self.mongo = self.config.mongo
            if force_version is None:
                self.version = CHAIN.get_version_for_height(index)
            else:
                self.version = force_version
            if force_time:
                self.time = str(int(force_time))
            else:
                self.time = str(int(time.time()))
            blocks = self.config.BU.get_blocks()
            self.index = index
            if self.index == 0:
                self.prev_hash = ''
            else:
                self.prev_hash = self.config.BU.get_latest_block()['hash']
            self.public_key = public_key
            self.private_key = private_key

            transaction_objs = []
            fee_sum = 0.0
            unspent_indexed = {}
            unspent_fastgraph_indexed = {}
            used_sigs = []
            for txn in transactions:
                try:
                    if isinstance(txn, Transaction):
                        transaction_obj = txn
                    else:
                        transaction_obj = Transaction.from_dict(self.index, txn)

                    if transaction_obj.transaction_signature in used_sigs:
                        print('duplicate transaction found and removed')
                        continue

                    used_sigs.append(transaction_obj.transaction_signature)
                    transaction_obj.verify()

                    if not isinstance(transaction_obj, FastGraph) and transaction_obj.rid:
                        for input_id in transaction_obj.inputs:
                            input_block = self.config.BU.get_transaction_by_id(input_id.id, give_block=True)
                            if input_block and input_block['index'] > (self.config.BU.get_latest_block()['index'] - 2016):
                                continue

                except:
                    try:
                        if isinstance(txn, FastGraph):
                            transaction_obj = txn
                        else:
                            transaction_obj = FastGraph.from_dict(self.index, txn)

                        if transaction_obj.transaction.transaction_signature in used_sigs:
                            print('duplicate transaction found and removed')
                            continue
                        used_sigs.append(transaction_obj.transaction.transaction_signature)
                        if not transaction_obj.verify():
                            raise InvalidTransactionException("invalid transactions")
                        transaction_obj = transaction_obj.transaction
                    except:
                        raise InvalidTransactionException("invalid transactions")

                address = str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(transaction_obj.public_key)))
                #check double spend
                if address in unspent_indexed:
                    unspent_ids = unspent_indexed[address]
                else:
                    res = self.config.BU.get_wallet_unspent_transactions(address)
                    unspent_ids = [x['id'] for x in res]
                    unspent_indexed[address] = unspent_ids

                failed = False
                used_ids_in_this_txn = []

                for x in transaction_obj.inputs:
                    if x.id not in unspent_ids:
                        if isinstance(x, ExternalInput):
                            txn2 = self.config.BU.get_transaction_by_id(x.id, instance=True)
                            address2 = str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(txn2.public_key)))
                            res = self.config.BU.get_wallet_unspent_transactions(address2)
                            unspent_ids2 = [y['id'] for y in res]
                            if x.id not in unspent_ids2:
                                failed = True
                    if x.id in used_ids_in_this_txn:
                        failed = True
                    used_ids_in_this_txn.append(x.id)
                if not failed:
                    transaction_objs.append(transaction_obj)
                    fee_sum += float(transaction_obj.fee)
            block_reward = CHAIN.get_block_reward()
            coinbase_txn_fctry = TransactionFactory(
                self.index,
                public_key=self.public_key,
                private_key=self.private_key,
                outputs=[{
                    'value': block_reward + float(fee_sum),
                    'to': str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(self.public_key)))
                }],
                coinbase=True
            )
            coinbase_txn = coinbase_txn_fctry.generate_transaction()
            transaction_objs.append(coinbase_txn)

            self.transactions = transaction_objs
            txn_hashes = self.get_transaction_hashes()
            self.set_merkle_root(txn_hashes)
            self.block = Block(
                version=self.version,
                block_time=self.time,
                block_index=self.index,
                prev_hash=self.prev_hash,
                transactions=self.transactions,
                merkle_root=self.merkle_root,
                public_key=self.public_key
            )
        except Exception as e:
            import sys, os
            print("Exception {} BlockFactory".format(e))
            exc_type, exc_obj, exc_tb = sys.exc_info()
            fname = os.path.split(exc_tb.tb_frame.f_code.co_filename)[1]
            print(exc_type, fname, exc_tb.tb_lineno)
            raise

    @classmethod
    def generate_header(cls, block):
        if int(block.version) < 3:
            return str(block.version) + \
                str(block.time) + \
                block.public_key + \
                str(block.index) + \
                block.prev_hash + \
                '{nonce}' + \
                str(block.special_min) + \
                str(block.target) + \
                block.merkle_root
        else:
            # version 3 block do not contain special_min anymore and have target as 64 hex string
            return str(block.version) + \
                   str(block.time) + \
                   block.public_key + \
                   str(block.index) + \
                   block.prev_hash + \
                   '{nonce}' + \
                   hex(block.target)[2:].rjust(64, '0') + \
                   block.merkle_root

    @classmethod
    def generate_hash_from_header(cls, header, nonce):
        header = header.format(nonce=nonce)
        return hashlib.sha256(hashlib.sha256(header.encode('utf-8')).digest()).digest()[::-1].hex()

    def get_transaction_hashes(self):
        return sorted([str(x.hash) for x in self.transactions], key=str.lower)

    def set_merkle_root(self, txn_hashes):
        hashes = []
        for i in range(0, len(txn_hashes), 2):
            txn1 = txn_hashes[i]
            try:
                txn2 = txn_hashes[i+1]
            except:
                txn2 = ''
            hashes.append(hashlib.sha256((txn1+txn2).encode('utf-8')).digest().hex())
        if len(hashes) > 1:
            self.set_merkle_root(hashes)
        else:
            self.merkle_root = hashes[0]

    @classmethod
    def get_target(cls, height, last_block, block, blockchain):
        # change target
        max_target = CHAIN.MAX_TARGET
        max_block_time = CHAIN.target_block_time(get_config().network)
        retarget_period = CHAIN.RETARGET_PERIOD  # blocks
        two_weeks = CHAIN.TWO_WEEKS  # seconds
        half_week = CHAIN.HALF_WEEK  # seconds
        if height > 0 and height % retarget_period == 0:
            block_from_2016_ago = Block.from_dict(get_config().BU.get_block_by_index(height - retarget_period))
            two_weeks_ago_time = block_from_2016_ago.time
            elapsed_time_from_2016_ago = int(last_block.time) - int(two_weeks_ago_time)
            # greater than two weeks?
            if elapsed_time_from_2016_ago > two_weeks:
                time_for_target = two_weeks
            elif elapsed_time_from_2016_ago < half_week:
                time_for_target = half_week
            else:
                time_for_target = int(elapsed_time_from_2016_ago)

            block_to_check = last_block
                
            if blockchain.partial:
                start_index = len(blockchain.blocks) - 1
            else:
                start_index = last_block.index
            while 1:
                if block_to_check.special_min or block_to_check.target == max_target or not block_to_check.target:
                    block_to_check = blockchain.blocks[start_index]
                    start_index -= 1
                else:
                    target = block_to_check.target
                    break
            new_target = (time_for_target * target) / two_weeks
            if new_target > max_target:
                target = max_target
            else:
                target = new_target

        elif height == 0:
            target = max_target
        else:
            block_to_check = block
            if block.index >= 38600 and (int(block.time) - int(last_block.time)) > max_block_time:
                target_factor = (int(block.time) - int(last_block.time)) / max_block_time
                target = block.target * (target_factor * 4)
                if target > max_target:
                    return max_target
                return target
            block_to_check = last_block  # this would be accurate. right now, it checks if the current block is under its own target, not the previous block's target

            if blockchain.partial:
                start_index = len(blockchain.blocks) - 1
            else:
                start_index = last_block.index
            while 1:
                if block_to_check.special_min or block_to_check.target == max_target or not block_to_check.target:
                    block_to_check = blockchain.blocks[start_index]
                    start_index -= 1
                else:
                    target = block_to_check.target
                    break
        return target

    @classmethod
    def mine(cls, header, target, nonces, special_min=False):

        lowest = (CHAIN.MAX_TARGET, 0, '')
        nonce = nonces[0]
        while nonce < nonces[1]:
            hash_test = cls.generate_hash_from_header(header, str(nonce))

            text_int = int(hash_test, 16)
            if text_int < target or special_min:
                return nonce, hash_test

            if text_int < lowest[0]:
                lowest = (text_int, nonce, hash_test)
            nonce += 1
        return lowest[1], lowest[2]

    @classmethod
    def get_genesis_block(cls):
        return Block.from_dict({
            "nonce" : 0,
            "hash" : "0dd0ec9ab91e9defe535841a4c70225e3f97b7447e5358250c2dc898b8bd3139",
            "public_key" : "03f44c7c4dca3a9204f1ba284d875331894ea8ab5753093be847d798274c6ce570",
            "id" : "MEUCIQDDicnjg9DTSnGOMLN3rq2VQC1O9ABDiXygW7QDB6SNzwIga5ri7m9FNlc8dggJ9sDg0QXUugrHwpkVKbmr3kYdGpc=",
            "merkleRoot" : "705d831ced1a8545805bbb474e6b271a28cbea5ada7f4197492e9a3825173546",
            "index" : 0,
            "target" : "fffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff",
            "special_min" : False,
            "version" : "1",
            "transactions" : [ 
                {
                    "public_key" : "03f44c7c4dca3a9204f1ba284d875331894ea8ab5753093be847d798274c6ce570",
                    "fee" : 0.0000000000000000,
                    "hash" : "71429326f00ba74c6665988bf2c0b5ed9de1d57513666633efd88f0696b3d90f",
                    "dh_public_key" : "",
                    "relationship" : "",
                    "inputs" : [],
                    "outputs" : [ 
                        {
                            "to" : "1iNw3QHVs45woB9TmXL1XWHyKniTJhzC4",
                            "value" : 50.0000000000000000
                        }
                    ],
                    "rid" : "",
                    "id" : "MEUCIQDZbaCDMmJJ+QJHldj1EWu0yG7enlwRAXoO1/B617KaxgIgBLB4L2ICWpDZf5Eo2bcXgUmKd91ayrOG/6jhaIZAPb0="
                }
            ],
            "time" : "1537127756",
            "prevHash" : ""
        })


class Block(object):

    # Memory optimization
    __slots__ = ('config', 'mongo', 'version', 'time', 'index', 'prev_hash', 'nonce', 'transactions', 'txn_hashes',
                 'merkle_root', 'verify_merkle_root','hash', 'public_key', 'signature', 'special_min', 'target',
                 'header')
    
    def __init__(
        self,
        version=0,
        block_time=0,
        block_index=-1,
        prev_hash='',
        nonce:str='',
        transactions=None,
        block_hash='',
        merkle_root='',
        public_key='',
        signature='',
        special_min: bool=False,
        header= '',
        target: int=0
    ):
        self.config = get_config()
        self.mongo = self.config.mongo
        self.version = version
        self.time = block_time
        self.index = block_index
        self.prev_hash = prev_hash
        self.nonce = nonce
        self.transactions = transactions
        txn_hashes = self.get_transaction_hashes()
        self.set_merkle_root(txn_hashes)
        self.merkle_root = merkle_root
        self.verify_merkle_root = ''
        self.hash = block_hash
        self.public_key = public_key
        self.signature = signature
        self.special_min = special_min
        self.target = target
        if target==0:
            # Same call as in new block check - but there's a circular reference here.
            self.target = BlockFactory.get_target(self.index, Block.from_dict(self.config.BU.get_latest_block()), self,
                                                  self.config.consensus.existing_blockchain)
        self.header = header

    @classmethod
    def from_dict(cls, block):
        transactions = []
        for txn in block.get('transactions'):
            # TODO: do validity checking for coinbase transactions
            if str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(block.get('public_key')))) in [x['to'] for x in txn.get('outputs', '')] and len(txn.get('outputs', '')) == 1 and not txn.get('inputs') and not txn.get('relationship'):
                txn['coinbase'] = True  
            else:
                txn['coinbase'] = False
            if 'signatures' in txn:
                transactions.append(FastGraph.from_dict(block.get('index'), txn))
            else:
                transactions.append(Transaction.from_dict(block.get('index'), txn))

        return cls(
            version=block.get('version'),
            block_time=block.get('time'),
            block_index=block.get('index'),
            public_key=block.get('public_key'),
            prev_hash=block.get('prevHash'),
            nonce=block.get('nonce'),
            transactions=transactions,
            block_hash=block.get('hash'),
            merkle_root=block.get('merkleRoot'),
            signature=block.get('id'),
            special_min=block.get('special_min'),
            header=block.get('header', ''),
            target=int(block.get('target'), 16)
        )
    
    def get_coinbase(self):
        for txn in self.transactions:
            if str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(self.public_key))) in [x.to for x in txn.outputs] and len(txn.outputs) == 1 and not txn.relationship and len(txn.inputs) == 0:
                return txn

    def verify(self):
        getcontext().prec = 8
        if int(self.version) != int(CHAIN.get_version_for_height(self.index)):
            raise Exception("Wrong version for block height", self.version, CHAIN.get_version_for_height(self.index))

        txns = self.get_transaction_hashes()
        self.set_merkle_root(txns)
        if self.verify_merkle_root != self.merkle_root:
            raise Exception("Invalid block merkle root")

        header = BlockFactory.generate_header(self)
        hashtest = BlockFactory.generate_hash_from_header(header, str(self.nonce))
        # print("header", header, "nonce", self.nonce, "hashtest", hashtest)
        if self.hash != hashtest:
            getLogger("tornado.application").warning("Verify error hashtest {} header {} nonce {}".format(hashtest, header, self.nonce))
            raise Exception('Invalid block hash')

        address = P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(self.public_key))
        try:
            # print("address", address, "sig", self.signature, "pubkey", self.public_key)
            result = verify_signature(base64.b64decode(self.signature), self.hash.encode('utf-8'), bytes.fromhex(self.public_key))
            if not result:
                raise Exception("block signature1 is invalid")
        except:
            try:
                result = VerifyMessage(address, BitcoinMessage(self.hash.encode('utf-8'), magic=''), self.signature)
                if not result:
                    raise
            except:
                raise Exception("block signature2 is invalid")

        # verify reward
        coinbase_sum = 0
        for txn in self.transactions:
            if txn.coinbase:
                for output in txn.outputs:
                    coinbase_sum += float(output.value)

        fee_sum = 0.0
        for txn in self.transactions:
            if not txn.coinbase:
                fee_sum += float(txn.fee)
        reward = CHAIN.get_block_reward(self.index)

        #if Decimal(str(fee_sum)[:10]) != Decimal(str(coinbase_sum)[:10]) - Decimal(str(reward)[:10]):
        """
        KO for block 13949
        0.02099999 50.021 50.0
        Integrate block error 1 ('Coinbase output total does not equal block reward + transaction fees', 0.020999999999999998, 0.021000000000000796)
        """
        if quantize_eight(fee_sum) != quantize_eight(coinbase_sum - reward):
            print(fee_sum, coinbase_sum, reward)
            raise Exception("Coinbase output total does not equal block reward + transaction fees", fee_sum, (coinbase_sum - reward))

    def get_transaction_hashes(self):
        """Returns a sorted list of tx hash, so the merkle root is constant across nodes"""
        return sorted([str(x.hash) for x in self.transactions], key=str.lower)

    def set_merkle_root(self, txn_hashes):
        hashes = []
        for i in range(0, len(txn_hashes), 2):
            txn1 = txn_hashes[i]
            try:
                txn2 = txn_hashes[i+1]
            except:
                txn2 = ''
            hashes.append(hashlib.sha256((txn1+txn2).encode('utf-8')).digest().hex())
        if len(hashes) > 1:
            self.set_merkle_root(hashes)
        else:
            self.verify_merkle_root = hashes[0]

    def save(self):
        self.verify()
        for txn in self.transactions:
            if txn.inputs:
                address = str(P2PKHBitcoinAddress.from_pubkey(bytes.fromhex(txn.public_key)))
                unspent = self.config.BU.get_wallet_unspent_transactions(address, [x.id for x in txn.inputs])
                unspent_ids = [x['id'] for x in unspent]
                failed = False
                used_ids_in_this_txn = []
                for x in txn.inputs:
                    if x.id not in unspent_ids:
                        failed = True
                    if x.id in used_ids_in_this_txn:
                        failed = True
                    used_ids_in_this_txn.append(x.id)
                if failed:
                    raise Exception('double spend', [x.id for x in txn.inputs])
        res = self.mongo.db.blocks.find({"index": (int(self.index) - 1)})
        if res.count() and res[0]['hash'] == self.prev_hash or self.index == 0:
            self.mongo.db.blocks.insert(self.to_dict())
        else:
            print("CRITICAL: block rejected...")

    def delete(self):
        self.mongo.db.blocks.remove({"index": self.index})

    def to_dict(self):
        return {
            'version': self.version,
            'time': self.time,
            'index': self.index,
            'public_key': self.public_key,
            'prevHash': self.prev_hash,
            'nonce': self.nonce,
            'transactions': [x.to_dict() for x in self.transactions],
            'hash': self.hash,
            'merkleRoot': self.merkle_root,
            'special_min': self.special_min,
            'target': hex(self.target)[2:].rjust(64, '0'),
            'header': self.header,
            'id': self.signature
        }

    def to_json(self):
        return json.dumps(self.to_dict(), indent=4)
