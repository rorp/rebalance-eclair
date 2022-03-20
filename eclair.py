import time
from functools import lru_cache

import requests
from requests.auth import HTTPBasicAuth


class EclairRPCException(Exception):
    pass


class RouteNotFoundException(Exception):
    pass


class Audit:
    def __init__(self, json):
        self.sent = []
        self.received = []
        self.relayed = []

        if 'sent' in json and isinstance(json['sent'], list):
            self.sent = json['sent']
        if 'received' in json and isinstance(json['received'], list):
            self.received = json['received']
        if 'relayed' in json and isinstance(json['relayed'], list):
            self.relayed = json['relayed']


class Failure:
    def __init__(self, code, errorMessages):
        self.code = code
        self.errorMessages = errorMessages
        self.failure_source_index = 0

    def error_message(self):
        return ", ".join(self.errorMessages)


class PayInvoiceResponse:
    def __init__(self, json):
        self.payment_hash = json['paymentHash']
        self.payment_preimage = json['status'].get('paymentPreimage')
        self.id = json.get('parentId')
        status = json['status']
        self.failures = []
        self.failed_node = None
        self.failed_channel = None
        if 'failures' in status:
            self.failures = status['failures']
            for f in self.failures:
                if self.failed_node is None:
                    if 'failedNode' in f:
                        self.failed_node = f['failedNode']
                    for hop in f['failedRoute']:
                        if self.failed_node == hop['nodeId']:
                            self.failed_channel = hop['shortChannelId']
                            break
            self.failure = Failure(-1, [j['failureMessage'] for j in self.failures])
        else:
            self.failure = Failure(0, '')


class ChannelDesc:
    def __init__(self, chan_id, node1_pub, node2_pub):
        self.chan_id = chan_id
        self.node1_pub = node1_pub
        self.node2_pub = node2_pub


class Edge:
    def __init__(self, chan_id, node1_pub, node2_pub, node1_policy, node2_policy):
        self.chan_id = chan_id
        self.node1_pub = node1_pub
        self.node2_pub = node2_pub
        self.node1_policy = node1_policy
        self.node2_policy = node2_policy


class RoutingPolicy:
    def __init__(self, json):
        self.time_lock_delta = json['cltvExpiryDelta']
        self.min_htlc = json['htlcMinimumMsat']
        self.fee_base_msat = json['feeBaseMsat']
        self.fee_rate_milli_msat = json['feeProportionalMillionths']
        self.disabled = not json['channelFlags']['isEnabled']
        self.max_htlc_msat = json['htlcMaximumMsat']
        self.last_update = json['timestamp']


class Channel:
    def __init__(self, json):
        data = json['data']
        commitments = data['commitments']
        local_params = commitments['localParams']
        remote_params = commitments['remoteParams']
        local_commit = commitments['localCommit']
        to_local = local_commit['spec']['toLocal']
        to_remote = local_commit['spec']['toRemote']

        self.remote_pubkey = json['nodeId']
        self.local_pubkey = local_params['nodeId']
        self.channel_id = json['channelId']

        self.local_balance = int(to_local / 1000)
        self.local_chan_reserve_sat = local_params['channelReserve']
        self.remote_balance = int(to_remote / 1000)
        self.remote_chan_reserve_sat = remote_params['channelReserve']
        self.capacity = self.local_balance + self.remote_balance

        self.chan_point = commitments['commitInput']['outPoint']
        self.channel_point = self.chan_point

        self.channel_update = None
        self.chan_id = self.channel_id
        if 'channelUpdate' in data:
            channel_update = data['channelUpdate']
            self.chan_id = channel_update['shortChannelId']
            self.fee_base_msat = channel_update['feeBaseMsat']
            self.fee_rate_milli_msat = channel_update['feeProportionalMillionths']

            if channel_update['channelFlags']['isNode1']:
                self.node1_pub = self.local_pubkey
                self.node2_pub = self.remote_pubkey
            else:
                self.node1_pub = self.remote_pubkey
                self.node2_pub = self.local_pubkey
            self.channel_update = channel_update

    def __repr__(self):
        return f"{self.chan_id}:{self.node1_pub}:{self.node2_pub}"

    def to_hop(self, amt_to_forward_msat, fee_msat, first):
        if first:
            source_pub_key = self.local_pubkey
            pub_key = self.remote_pubkey
        else:
            source_pub_key = self.remote_pubkey
            pub_key = self.local_pubkey
        return Hop(source_pub_key, pub_key, self.chan_id, self.capacity, amt_to_forward_msat, fee_msat)


class Invoice:
    def __init__(self, json):
        self.destination = json['nodeId']
        self.payment_hash = json['paymentHash']
        self.num_msat = json['amount']
        self.num_satoshis = int(self.num_msat / 1000)
        self.timestamp = json['timestamp']
        self.expiry = json['expiry']
        self.description = json['description']
        self.cltv_expiry = json['minFinalCltvExpiry']
        self.serialized = json['serialized']


class Hop:
    def __init__(self, source_pub_key, pub_key, chan_id, chan_capacity, amt_to_forward_msat, fee_msat):
        self.source_pub_key = source_pub_key
        self.pub_key = pub_key
        self.chan_id = chan_id
        self.chan_capacity = chan_capacity
        self.amt_to_forward_msat = amt_to_forward_msat
        self.amt_to_forward = int(amt_to_forward_msat / 1000)
        self.fee_msat = fee_msat
        self.fee = int(fee_msat / 1000)


class Route:
    def __init__(self, amount_msat, hops):
        self.hops = hops
        self.total_fees_msat = sum([hop.fee_msat for hop in hops])
        self.total_fees = int(self.total_fees_msat / 1000)
        self.total_amt_msat = amount_msat + self.total_fees_msat
        self.total_amt = int(self.total_amt_msat / 1000)


class Eclair:
    def __init__(self, conf, address, password):
        if address:
            self.address = address
            self.password = password
        else:
            port = conf.get_string('eclair.api.port', '8080')
            self.address = f"localhost:{port}"
            self.password = conf.get_string('eclair.api.password')

    def parse_channel_id(self, id_string):
        return id_string

    @lru_cache(maxsize=None)
    def get_info(self):
        return self.call_eclair("getinfo")

    @lru_cache(maxsize=None)
    def get_node_info(self, pub_key):
        params = {'nodeIds': pub_key}
        json = self.call_eclair("nodes", params)
        return json

    def get_node_alias(self, pub_key):
        if self.get_own_pubkey() == pub_key:
            return self.get_info()['alias']
        else:
            infos = self.get_node_info(pub_key)
            if len(infos) > 0:
                return infos[0]['alias']
            else:
                return None

    def get_own_pubkey(self):
        return self.get_info()['nodeId']

    @lru_cache(maxsize=None)
    def get_peers(self):
        return self.call_eclair("peers")

    @lru_cache(maxsize=None)
    def get_all_updates(self, pub_key):
        res = self.call_eclair("allupdates", {'nodeId': pub_key})
        return res

    def get_channel_update(self, pub_key, channel_id):
        if pub_key == self.get_own_pubkey():
            return self.get_channel(channel_id).channel_update
        else:
            for update in self.get_all_updates(pub_key):
                if update['shortChannelId'] == channel_id:
                    return update
            return None

    def generate_invoice(self, memo, amount):
        params = {
            "description": memo,
            "amountMsat": amount * 1000
        }
        return Invoice(self.call_eclair("createinvoice", params))

    def cancel_invoice(self, payment_hash):
        return self.call_eclair("deleteinvoice", {'paymentHash': payment_hash})

    def send_payment(self, payment_request, route):
        params = {
            'shortChannelIds': ",".join([hop.chan_id for hop in route.hops]),
            'amountMsat': payment_request.num_msat,
            'invoice': payment_request.serialized,
            'finalCltvExpiry': payment_request.cltv_expiry,
        }
        payment = self.call_eclair("sendtoroute", params)
        payment_id = payment['parentId']
        tries = 0
        while tries < 240:
            res = self.call_eclair("getsentinfo", {'id': payment_id})
            if len(res) > 0 and res[0]['status']['type'] != 'pending':
                return PayInvoiceResponse(res[0])
            time.sleep(1)
            tries = tries + 1
        raise Exception('Cannot get sent info: too many tries')

    def decode_payment_request(self, payment_request):
        params = {
            "invoice": payment_request,
        }
        return self.call_eclair("parseinvoice", params)

    @lru_cache(maxsize=None)
    def get_audit(self, frm=0, to=99999999999):
        return Audit(self.call_eclair("audit", {'from': frm, 'to': to}))

    @lru_cache(maxsize=None)
    def get_channels(self, active_only=False):
        json = self.call_eclair("channels")
        filtered = [Channel(ch) for ch in json if ch["state"] == "NORMAL"]
        return sorted(filtered, key=lambda ch: ch.chan_id)

    def get_channel(self, channel_id):
        for ch in self.get_channels():
            if ch.chan_id == channel_id:
                return ch
        return None

    @lru_cache(maxsize=None)
    def get_edges(self):
        return self.call_eclair("allchannels")

    def get_edge(self, chan_id):
        node1_pub = None
        node2_pub = None
        channel = self.get_channel(chan_id)
        if channel is None:
            for edge in self.get_edges():
                if edge['shortChannelId'] == chan_id:
                    node1_pub = edge['a']
                    node2_pub = edge['b']
                    break
        else:
            node1_pub = channel.node1_pub
            node2_pub = channel.node2_pub

        if node1_pub is None or node2_pub is None:
            return None

        node1_policy = None
        channel_update1 = self.get_channel_update(node1_pub, chan_id)
        if channel_update1:
            node1_policy = RoutingPolicy(channel_update1)
        node2_policy = None
        channel_update2 = self.get_channel_update(node2_pub, chan_id)
        if channel_update2:
            node2_policy = RoutingPolicy(channel_update2)
        return Edge(chan_id, node1_pub, node2_pub, node1_policy, node2_policy)

    def get_policy_to(self, channel_id):
        edge = self.get_edge(channel_id)
        # node1_policy contains the fee base and rate for payments from node1 to node2
        if edge.node1_pub == self.get_own_pubkey():
            return edge.node1_policy
        return edge.node2_policy

    def get_policy_from(self, channel_id):
        edge = self.get_edge(channel_id)
        # node1_policy contains the fee base and rate for payments from node1 to node2
        if edge.node1_pub == self.get_own_pubkey():
            return edge.node2_policy
        return edge.node1_policy

    def get_ppm_to(self, channel_id):
        return self.get_policy_to(channel_id).fee_rate_milli_msat

    def get_ppm_from(self, channel_id):
        policy = self.get_policy_from(channel_id)
        if policy:
            return policy.fee_rate_milli_msat
        return None

    @lru_cache(maxsize=None)
    def get_max_channel_capacity(self):
        max_channel_capacity = 0
        for channel in self.get_channels(active_only=False):
            if channel.capacity > max_channel_capacity:
                max_channel_capacity = channel.capacity
        return max_channel_capacity

    def get_route(
            self,
            first_hop_channel,
            last_hop_channel,
            amount,
            ignored_pairs,
            ignored_nodes,
            fee_limit_msat,
    ):
        ignore_channel_ids = [p['chan_id'] for p in ignored_pairs]

        ignore_node_ids = []

        if isinstance(ignored_nodes, list):
            ignore_node_ids = ignore_node_ids + ignored_nodes

        if first_hop_channel and last_hop_channel:
            first_hop_channels = [first_hop_channel]
            last_hop_channels = [last_hop_channel]
        elif first_hop_channel:
            first_hop_channels = [first_hop_channel]
            last_hop_channels = [chan for chan in self.get_channels(active_only=True) if
                                 chan.chan_id != first_hop_channel.chan_id]
        elif last_hop_channel:
            last_hop_channels = [last_hop_channel]
            first_hop_channels = [chan for chan in self.get_channels(active_only=True) if
                                  chan.chan_id != last_hop_channel.chan_id]
        else:
            return []

        routes = []
        for first_hop in first_hop_channels:
            for last_hop in last_hop_channels:
                found_routes = self.find_route(first_hop, last_hop, amount, fee_limit_msat, ignore_node_ids,
                                               ignore_channel_ids)
                for route in found_routes:
                    routes.append(route)
        return routes

    def find_route(self, first_hop_channel, last_hop_channel, amount, fee_limit_msat, ignore_node_ids,
                   ignore_channel_ids):
        if first_hop_channel.chan_id in ignore_channel_ids:
            return []
        if last_hop_channel.chan_id in ignore_channel_ids:
            return []

        last_hop_pubkey = last_hop_channel.remote_pubkey
        first_hop_pubkey = self.get_own_pubkey()
        amount_msat = int(amount * 1000)
        last_hop_fee = self.calc_fees_msat(amount_msat, last_hop_channel.chan_id)
        if fee_limit_msat:
            fee_limit = int(fee_limit_msat) - last_hop_fee
        else:
            fee_limit = None

        local_channel_ids = [chan.chan_id for chan in self.get_channels(active_only=False) if
                             chan.chan_id != first_hop_channel.chan_id]

        params = {
            'sourceNodeId': first_hop_pubkey,
            'targetNodeId': last_hop_pubkey,
            'amountMsat': int(amount * 1000),
            'format': 'full',
            'ignoreNodeIds': self.empty_to_none(",".join(ignore_node_ids)),
            'ignoreShortChannelIds': self.empty_to_none(",".join(
                self.concat(local_channel_ids, ignore_channel_ids))),
            'maxFeeMsat': fee_limit,
        }
        try:
            found_routes = self.call_eclair("findroutebetweennodes", params)
            routes = []
            if len(found_routes['routes']) > 0:
                found_route = found_routes['routes'][0]
                hops = [self.route_to_hop(hop, amount_msat) for hop in found_route['hops']]
                if hops[0].chan_id != first_hop_channel.chan_id:
                    raise EclairRPCException('Route starts with unexpected channel: ' + hops[0].chan_id)
                if last_hop_channel:
                    hops.append(last_hop_channel.to_hop(amount_msat, last_hop_fee, first=False))
                routes.append(Route(amount_msat, hops))
            return routes
        except RouteNotFoundException:
            return []

    def calc_fees_msat(self, amount_msat, chan_id):
        return int(self.get_policy_from(chan_id).fee_base_msat + amount_msat * self.get_ppm_from(chan_id) / 1_000_000)

    @staticmethod
    def route_to_hop(hop, amt_to_forward_msat):
        last_update = hop['lastUpdate']
        fee_rate_milli_msat = last_update['feeProportionalMillionths']
        fee_base_msat = last_update['feeBaseMsat']
        fee_msat = int(amt_to_forward_msat / 1_000_000 * fee_rate_milli_msat + fee_base_msat)
        return Hop(hop['nodeId'], hop['nextNodeId'], last_update['shortChannelId'],
                   int(last_update['htlcMaximumMsat'] / 1000),
                   amt_to_forward_msat,
                   fee_msat)

    def call_eclair(self, endpoint, payload={}):
        url = f"http://{self.address}/{endpoint}"
        res = requests.request("POST", url, auth=HTTPBasicAuth("eclair-cli", self.password), data=payload).json()
        if 'error' in res:
            if res['error'] in ['route not found', 'balance too low']:
                raise RouteNotFoundException(res['error'])
            else:
                raise EclairRPCException(res['error'])
        return res

    @staticmethod
    def empty_to_none(str):
        if str == '':
            return None
        else:
            return str

    @staticmethod
    def concat(l1, l2):
        res = [x for x in l1]
        for x in l2:
            if not x in res:
                res.append(x)
        return res
