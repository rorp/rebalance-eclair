#!/usr/bin/env python3

import json
from eclair import Eclair
from eclair import Audit
from eclair import Channel
from eclair import Peer
from output import Output, format_alias, format_ppm, format_amount, format_amount_green, format_boring_string, \
    print_bar, format_channel_id, format_error
import os
from os.path import expanduser
from eclair import Eclair
from pyhocon import ConfigFactory
import sys
import time


def read_eclair_config(dir):
    dir = expanduser(dir)
    file = f"{dir}/eclair.conf"
    if os.path.exists(file):
        return ConfigFactory.parse_file(file)
    else:
        return None


def received_timestamp(received):
    max_timestamp = 0
    max_iso = ''
    for part in received['parts']:
        unix = part['timestamp']['unix']
        iso = part['timestamp']['iso']
        if unix > max_timestamp:
            max_timestamp = unix
            max_iso = iso
    return [max_timestamp, max_iso]


def received_amount(received):
    amount = 0
    for part in received['parts']:
        amount = amount + part['amount']
    return amount


def received_channel_id(received):
    ch = None
    for part in received['parts']:
        if not ch is None and ch != part['fromChannelId']:
            raise Exception('multipart payment')
        ch = part['fromChannelId']
    return ch


def sent_timestamp(sent):
    min_timestamp = 9999999999
    min_iso = ''
    for part in sent['parts']:
        unix = part['timestamp']['unix']
        iso = part['timestamp']['iso']
        if unix < min_timestamp:
            min_timestamp = unix
            min_iso = iso
    return [min_timestamp, min_iso]


def sent_amount(sent):
    amount = 0
    for part in sent['parts']:
        amount = amount + part['amount']
    return amount


def sent_fees(sent):
    fees = 0
    for part in sent['parts']:
        fees = fees + part['feesPaid']
    return fees


def sent_channel_id(sent):
    ch = None
    for part in sent['parts']:
        if not ch is None and ch != part['toChannelId']:
            raise Exception('multipart payment')
        ch = part['toChannelId']
    return ch

all_aliases = {}

def get_all_aliases():
    if not all_aliases:
        nodes = eclair.get_nodes()
        for node in nodes:
            all_aliases[node['nodeId']] = node['alias']
    return all_aliases

def get_alias(aliases, channels, node_id, channel_id):
    if node_id in aliases and aliases[node_id] != node_id:
        return aliases[node_id]
    elif node_id in get_all_aliases():
        return get_all_aliases()[node_id]
    elif channel_id in channels:
        return channels[channel_id].chan_id
    else:
        return node_id

def sort_by_received_timestamp(pair):
    rcvd = pair[1]
    return received_timestamp(rcvd)[0]

from_ts = int(time.time()) - 60 * 60 * 24 * 31
if len(sys.argv) == 2:
    from_ts = int(sys.argv[1])

eclair_conf = read_eclair_config('~/.eclair')
eclair = Eclair(eclair_conf, None, None)

node_ids = {}
chan_list = eclair.get_channels(active_only=False, public_only=False)
try:
    closed_chan_list = eclair.get_closed_channels()
except EclairRPCException:
    closed_chan_list = []
chan_list = chan_list + closed_chan_list
channels = {}
for ch in chan_list:
    channels[ch.channel_id] = ch
    node_ids[ch.channel_id] = ch.remote_pubkey

peer_list = eclair.get_peers()
aliases = {}
for p in peer_list:
    aliases[p.pub_key] = p.alias

audit = eclair.get_audit(frm=from_ts)

sent = {}
for s in audit.sent:
    sent[s['paymentHash']] = s

received = {}
for r in audit.received:
    received[r['paymentHash']] = r

rebalanced = []
for receivedHash in received:
    if receivedHash in sent:
        rebalanced.append([sent[receivedHash], received[receivedHash]])

rebalanced.sort(key=sort_by_received_timestamp)

print(f"{'=' * 71} Rebalanced {'=' * 75}")

for pair in rebalanced:
    sent = pair[0]
    rcvd = pair[1]
    rcvd_ts = received_timestamp(rcvd)
    sent_ts = sent_timestamp(sent)
    ts = rcvd_ts[1].ljust(25)
    latency = sent_ts[0] - rcvd_ts[0]
    sent_amt = sent_amount(sent)
    fees = sent_fees(sent)
    received_amt = received_amount(rcvd)
    to_ch = sent_channel_id(sent)
    from_ch = received_channel_id(rcvd)
    from_node_id = node_ids.get(from_ch, from_ch)
    to_node_id = node_ids.get(to_ch, to_ch)
    from_node = get_alias(aliases, channels, from_node_id, from_ch).ljust(32)
    to_node = get_alias(aliases, channels, to_node_id, to_ch).ljust(32)
    print(
        f"{format_boring_string(ts)} "
        f"{format_alias(to_node)}\t"
        f"{format_alias(from_node)}\t"
        f"{format_amount(sent_amt, 16)}"
        f"{format_amount(received_amt, 16)}"
        f"{format_amount(fees, 10)} "
        f"{format_amount(latency, 10)}s"
    )

print(f"{'=' * 71} Relayed {'=' * 78}")

for r in audit.relayed:
    started_ts = r['startedAt']['iso'].ljust(25)
    settled_ts = r['settledAt']['iso'].ljust(25)
    ts = started_ts
    latency = r['settledAt']['unix'] - r['startedAt']['unix']
    amount = r['amountOut']
    fee = r['amountIn'] - r['amountOut']
    from_ch = r['fromChannelId']
    to_ch = r['toChannelId']
    from_node_id = node_ids.get(from_ch, from_ch)
    to_node_id = node_ids.get(to_ch, to_ch)
    from_node = get_alias(aliases, channels, from_node_id, from_ch).ljust(32)
    to_node = get_alias(aliases, channels, to_node_id, to_ch).ljust(32)
    print(
        f"{format_boring_string(ts)} "
        f"{format_alias(from_node)}\t"
        f"{format_alias(to_node)}\t"
        f"{format_amount(amount, 16)}"
        f"{format_amount(fee, 10)} "
        f"{format_amount(latency, 10)}s"
    )

