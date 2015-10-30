#! /usr/bin/env python3

import base64
import hmac
import io
import json
import os
import textwrap

import umsgpack
import nacl.bindings

# Hardcode the keys for everyone involved.
# ----------------------------------------

jack_private = b'\xaa' * 32
jack_public = nacl.bindings.crypto_scalarmult_base(jack_private)

max_private = b'\xbb' * 32
max_public = nacl.bindings.crypto_scalarmult_base(max_private)

chris_private = b'\xcc' * 32
chris_public = nacl.bindings.crypto_scalarmult_base(chris_private)


# Utility functions.
# ------------------

def random_key():
    return os.urandom(32)


def random_nonce():
    return os.urandom(24)


def chunks_with_empty(message):
    'The last chunk is empty, which signifies the end of the message.'
    chunk_size = 100
    chunk_start = 0
    chunks = []
    while chunk_start < len(message):
        chunks.append(message[chunk_start:chunk_start+chunk_size])
        chunk_start += chunk_size
    # empty chunk
    chunks.append(b'')
    return chunks


def write_framed_msgpack(stream, obj):
    msgpack_bytes = umsgpack.packb(obj)
    frame = umsgpack.packb(len(msgpack_bytes))
    stream.write(frame)
    stream.write(msgpack_bytes)


def read_framed_msgpack(stream):
    length = umsgpack.unpack(stream)
    print(length)
    # We discard the frame length and stream on.
    obj = umsgpack.unpack(stream)
    print(json_repr(obj))
    return obj


def json_repr(obj):
    # We need to repr everything that JSON doesn't directly support,
    # particularly bytes.
    def _recurse_repr(obj):
        if isinstance(obj, (list, tuple)):
            return [_recurse_repr(x) for x in obj]
        elif isinstance(obj, dict):
            return {_recurse_repr(key): _recurse_repr(val)
                    for key, val in obj.items()}
        elif isinstance(obj, bytes):
            return base64.b64encode(obj).decode()
        else:
            return obj
    return json.dumps(_recurse_repr(obj), indent='  ')


# All the important bits!
# -----------------------

def encode(sender_private, recipient_groups, message):
    output = io.BytesIO()
    sender_public = nacl.bindings.crypto_scalarmult_base(sender_private)
    session_key = random_key()
    mac_keys = []
    # We will skip MACs entirely if there's only going to be one MAC key. In
    # that case, Box() gives the same guarantees.
    need_macs = (len(recipient_groups) > 1)
    recipients_map = {}
    for groupnum, group in enumerate(recipient_groups):
        if need_macs:
            mac_key = random_key()
            mac_keys.append(mac_key)
        for recipient in group:
            per_recipient_map = {
                "session_key": session_key,
            }
            if need_macs:
                per_recipient_map["mac_group"] = groupnum
                per_recipient_map["mac_key"] = mac_key
            per_recipient_msgpack = umsgpack.packb(per_recipient_map)
            nonce = random_nonce()
            boxed_bytes = nacl.bindings.crypto_box(
                message=per_recipient_msgpack,
                nonce=nonce,
                sk=sender_private,
                pk=recipient)
            recipients_map[recipient] = nonce + boxed_bytes
    header_map = {
        "version": 1,
        "sender": sender_public,
        "recipients": recipients_map,
    }
    write_framed_msgpack(output, header_map)

    # Write the chunks.
    for chunknum, chunk in enumerate(chunks_with_empty(message)):
        nonce = chunknum.to_bytes(24, byteorder='big')
        # Box and strip the nonce.
        boxed_chunk = nacl.bindings.crypto_secretbox(
            message=chunk,
            nonce=nonce,
            key=session_key)
        chunk_map = {
            'chunk': boxed_chunk,
        }
        if need_macs:
            macs = []
            for mac_key in mac_keys:
                hmac_obj = hmac.new(mac_key, digestmod='sha512')
                hmac_obj.update(nonce)
                hmac_obj.update(boxed_chunk)
                macs.append(hmac_obj.digest()[:32])
            chunk_map['macs'] = macs
        write_framed_msgpack(output, chunk_map)

    return output.getvalue()


def decode(input, recipient_private):
    stream = io.BytesIO(input)
    header_map = read_framed_msgpack(stream)
    sender_public = header_map['sender']
    recipient_public = nacl.bindings.crypto_scalarmult_base(recipient_private)
    boxed_key_map = header_map['recipients'][recipient_public]
    key_map_msgpack = nacl.bindings.crypto_box_open(
        ciphertext=boxed_key_map[24:],
        nonce=boxed_key_map[:24],
        sk=recipient_private,
        pk=sender_public)
    key_map = umsgpack.unpackb(key_map_msgpack)
    print(textwrap.indent('key map: ' + json_repr(key_map), '### '))
    session_key = key_map['session_key']
    mac_key = key_map.get('mac_key')
    mac_group = key_map.get('mac_group')
    chunknum = 0
    output = io.BytesIO()
    while True:
        nonce = chunknum.to_bytes(24, byteorder='big')
        chunk_map = read_framed_msgpack(stream)
        boxed_chunk = chunk_map['chunk']
        # Check the MAC.
        if mac_key is not None:
            their_mac = chunk_map['macs'][mac_group]
            hmac_obj = hmac.new(mac_key, digestmod='sha512')
            hmac_obj.update(nonce)
            hmac_obj.update(boxed_chunk)
            our_mac = hmac_obj.digest()[:32]
            if not hmac.compare_digest(their_mac, our_mac):
                raise RuntimeError("MAC mismatch!")
        # Prepend the nonce and decrypt.
        chunk = nacl.bindings.crypto_secretbox_open(
            ciphertext=boxed_chunk,
            nonce=nonce,
            key=session_key)
        print('### chunk {}: {}'.format(chunknum, chunk))
        if chunk == b'':
            break
        output.write(chunk)
        chunknum += 1
    return output.getvalue()


def main():
    message = b'The Magic Words are Squeamish Ossifrage'
    output = encode(jack_private, [[max_public]], message)
    print(base64.b64encode(output).decode())
    print('-----------------------------------------')
    decoded_message = decode(output, max_private)
    print('message:', decoded_message)


if __name__ == '__main__':
    main()
