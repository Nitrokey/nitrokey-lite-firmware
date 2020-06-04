#! /usr/bin/python3

"""
upgrade_by_passwd.py - a tool to install another firmware for Gnuk Token
                       which is just shipped from factory

Copyright (C) 2012, 2013, 2015, 2018
              Free Software Initiative of Japan
Author: NIIBE Yutaka <gniibe@fsij.org>

This file is a part of Gnuk, a GnuPG USB Token implementation.

Gnuk is free software: you can redistribute it and/or modify it
under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

Gnuk is distributed in the hope that it will be useful, but WITHOUT
ANY WARRANTY; without even the implied warranty of MERCHANTABILITY
or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU General Public
License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""
import argparse
import binascii
import copy
import hashlib
import logging
import os
import time
from collections import defaultdict
from datetime import datetime
from enum import Enum
from functools import lru_cache
from getpass import getpass
from struct import pack
from subprocess import check_output

import requests

import rsa
from gnuk_token import get_gnuk_device, gnuk_devices_by_vidpid, \
    regnual, SHA256_OID_PREFIX, crc32, parse_kdf_data
from kdf_calc import kdf_calc
from usb_strings import get_devices, print_device

# This should be event driven, not guessing some period, or polling.
TIME_DETECT_DEVICE_AFTER_UPDATE_LONG_S = 5
TIME_DETECT_DEVICE_AFTER_UPDATE_S = 30
ERR_EMPTY_COUNTER = '6983'
ERR_INVALID_PIN = '6982'
DEFAULT_WAIT_FOR_REENUMERATION = 20
DEFAULT_PW3 = "12345678"
BY_ADMIN = 3
KEYNO_FOR_AUTH = 2
FORMAT = '%(relativeCreated)-8d %(message)s'
logging.basicConfig(format=FORMAT, level=logging.DEBUG, filename='upgrade.log')
logger = logging.getLogger()


def progress_func(x):
    x = x * 100
    if x == 0:
        progress_func.last = 0

    if progress_func.last * 10 <= x < 100:
        progress_func.last += 1
        print(f'Progress: {round(x, 2)}%\r', end='', flush=True)


progress_func.last = 0


def main(wait_e, keyno, passwd, data_regnual, data_upgrade, skip_bootloader, verbosity=0):
    reg = None
    for i in range(3):
        if reg is not None:
            break
        print('.', end='', flush=True)
        time.sleep(1)
        for dev in gnuk_devices_by_vidpid():
            try:
                reg = regnual(dev)
                if dev.filename:
                    print("Device: %s" % dev.filename)
                break
            except Exception as e:
                if str(e) != 'Wrong interface class':
                    print(e)

    if reg is None and not skip_bootloader:
        print('\n*** Starting bootloader upload procedure')
        l = len(data_regnual)
        if (l & 0x03) != 0:
            data_regnual = data_regnual.ljust(l + 4 - (l & 0x03), chr(0))
        crc32code = crc32(data_regnual)
        if verbosity:
            print("CRC32: %04x\n" % crc32code)
        data_regnual += pack('<I', crc32code)

        rsa_key = rsa.read_key_from_file('rsa_example.key')
        rsa_raw_pubkey = rsa.get_raw_pubkey(rsa_key)

        gnuk = get_gnuk_device()
        gnuk.cmd_select_openpgp()
        print('*** Connected to the device')
        # Compute passwd data
        try:
            kdf_data = gnuk.cmd_get_data(0x00, 0xf9).tobytes()
        except:
            kdf_data = b""
        if kdf_data == b"":
            passwd_data = passwd.encode('UTF-8')
        else:
            algo, subalgo, iters, salt_user, salt_reset, salt_admin, \
            hash_user, hash_admin = parse_kdf_data(kdf_data)
            if salt_admin:
                salt = salt_admin
            else:
                salt = salt_user
            passwd_data = kdf_calc(passwd, salt, iters)
        # And authenticate with the passwd data
        gnuk.cmd_verify(BY_ADMIN, passwd_data)
        gnuk.cmd_write_binary(1 + keyno, rsa_raw_pubkey, False)

        gnuk.cmd_select_openpgp()
        challenge = gnuk.cmd_get_challenge().tobytes()
        digestinfo = binascii.unhexlify(SHA256_OID_PREFIX) + challenge
        signed = rsa.compute_signature(rsa_key, digestinfo)
        signed_bytes = rsa.integer_to_bytes_256(signed)
        gnuk.cmd_external_authenticate(keyno, signed_bytes)
        gnuk.stop_gnuk()
        mem_info = gnuk.mem_info()
        if verbosity:
            print("%08x:%08x" % mem_info)

        print('*** Running update. Do NOT remove the device from the USB slot, until further notice.')

        print("Downloading flash upgrade program...")
        gnuk.download(mem_info[0], data_regnual, progress_func=progress_func)
        print("Run flash upgrade program...")
        gnuk.execute(mem_info[0] + len(data_regnual) - 4)
        #
        time.sleep(3)
        gnuk.reset_device()
        del gnuk
        gnuk = None

    if reg is None:
        print("Waiting for device to appear:")
        # while reg == None:
        print("  Wait {} second{}...".format(wait_e, 's' if wait_e > 1 else ''), end='')
        for i in range(wait_e):
            if reg is not None:
                break
            print('.', end='', flush=True)
            time.sleep(1)
            for dev in gnuk_devices_by_vidpid():
                try:
                    reg = regnual(dev)
                    if dev.filename:
                        print("Device: %s" % dev.filename)
                    break
                except Exception as e:
                    print(e)
                    pass
        print('')
        print('')
        if reg is None:
            print('Device not found. Exiting.')
            raise RuntimeWarning('Device not found. Exiting.')

    # Then, send upgrade program...
    mem_info = reg.mem_info()
    print("%08x:%08x" % mem_info)
    print("Downloading the program")
    reg.download(mem_info[0], data_upgrade, progress_func=progress_func)
    print("Protecting device")
    reg.protect()
    print("Finish flashing")
    reg.finish()
    print("Resetting device")
    reg.reset_device()
    print("Update procedure finished. Device could be removed from USB slot.")
    print('')
    return 0


@lru_cache()
def get_latest_release_data():
    try:
        r = requests.get('https://api.github.com/repos/Nitrokey/nitrokey-start-firmware/releases')
        json = r.json()
        if r.status_code == 403:
            logger.debug('JSON release data {}'.format(json))
            print('No Github API access')
            exit(3)
        latest_tag = json[0]
    except Exception as e:
        logger.exception('Failed getting release data')
        latest_tag = defaultdict(lambda: 'unknown')
    return latest_tag


def validate_binary_file(path: str):
    import os.path
    if not os.path.exists(path):
        raise argparse.ArgumentTypeError('Path does not exist: "{}"'.format(path))
    if not path.endswith('.bin'):
        raise argparse.ArgumentTypeError(
            'Supplied file "{}" does not have ".bin" extension. Make sure you are sending correct file to the device.'.format(
                os.path.basename(path)))
    return path


def validate_name(path: str, name: str):
    if name not in path:
        raise argparse.ArgumentTypeError(
            'Supplied file "{}" does not have "{}" in name. Make sure you have not swapped the arguments.'.format(
                os.path.basename(path), name))
    return path


def validate_gnuk(path: str):
    validate_binary_file(path)
    validate_name(path, 'gnuk')
    return path


def validate_regnual(path: str):
    validate_binary_file(path)
    validate_name(path, 'regnual')
    return path


def parse_arguments():
    parser = argparse.ArgumentParser(description='Update tool for GNUK')
    parser.add_argument('--regnual', type=validate_regnual, help='path to regnual binary', default=None)
    parser.add_argument('--gnuk', type=validate_gnuk, help='path to gnuk binary', default=None)
    parser.add_argument('-f', dest='default_password', action='store_true',
                        default=False, help='use default Admin PIN: {}'.format(DEFAULT_PW3))
    parser.add_argument('-p', dest='password',
                        help='use provided Admin PIN')
    parser.add_argument('-e', dest='wait_e', default=DEFAULT_WAIT_FOR_REENUMERATION, type=int,
                        help='time to wait for device to enumerate, after regnual was executed on device')
    parser.add_argument('-k', dest='keyno', default=0, type=int, help='selected key index')
    parser.add_argument('-v', dest='verbose', default=0, type=int, help='verbosity level')
    parser.add_argument('-y', dest='yes', default=False, action='store_true', help='agree to everything')
    parser.add_argument('-b', dest='skip_bootloader', default=False, action='store_true',
                        help='Skip bootloader upload (e.g. when done so already)')
    args = parser.parse_args()
    return args


def kill_smartcard_services():
    print('*** Could not connect to the device. Attempting to close scdaemon.')
    # check_output(["gpg-connect-agent",
    #               "SCD KILLSCD", "SCD BYE", "/bye"])

    commands = [['gpgconf', '--kill', 'all'],
                'sudo systemctl stop pcscd pcscd.socket'.split()]
    for command in commands:
        print('*** Running: "{}"'.format(' '.join(command)))
        logger.debug('Running {}'.format(command))
        try:
            check_output(command)
        except Exception as e:
            logger.exception('Error while running command')
    time.sleep(3)


class FirmwareType(Enum):
    UNKNOWN = 0
    REGNUAL = 1
    GNUK = 2
    CHECKSUM = 3


REMOTE_PATH = 'https://raw.githubusercontent.com/Nitrokey/nitrokey-start-firmware/gnuk1.2-regnual-fix/prebuilt'
FIRMWARE_URL = {
    FirmwareType.REGNUAL: ('%s/{}/regnual.bin' % REMOTE_PATH),
    FirmwareType.GNUK: ('%s/{}/gnuk.bin' % REMOTE_PATH),
    FirmwareType.CHECKSUM: ('%s/checksums.sha512' % REMOTE_PATH),
}


def hash_data_512(data):
    hash512 = hashlib.sha512(data).digest()
    hash512_hex = binascii.b2a_hex(hash512)
    return hash512_hex


def validate_hash(url: str, hash: bytes):
    checksums = download_file_or_exit(FIRMWARE_URL.get(FirmwareType.CHECKSUM, None))
    name = ' ' + '/'.join(url.split('/')[-2:])
    for line in checksums.splitlines():
        if name in line.decode():
            hash_expected, hash_name = line.split()
            logger.debug('{} {}/{} {}'.format(hash_expected == hash, hash_name, name, hash[-8:], hash_expected[-8:]))
            return hash_expected == hash
    return False


def get_firmware_file(file_name: str, type: FirmwareType):
    if file_name:
        with open(file_name, "rb") as f:
            firmware_data = f.read()
        print("- {}: {}".format(file_name, len(firmware_data)))
        return firmware_data

    tag = get_latest_release_data()['tag_name']
    url = FIRMWARE_URL.get(type, None).format(tag)
    firmware_data = download_file_or_exit(url)
    hash_data = hash_data_512(firmware_data)
    hash_valid = 'valid' if validate_hash(url, hash_data) else 'invalid'

    print(
        "- {}: {}, hash: ...{} {} (from ...{})".format(type, len(firmware_data), hash_data[-8:], hash_valid, url[-24:]))
    return firmware_data


@lru_cache()
def download_file_or_exit(url):
    resp = requests.get(url)
    if not resp.ok:
        print('Cannot download firmware data {}/{}: {}'.format('type', url, resp.status_code))
        exit(1)
    firmware_data = resp.content
    return firmware_data


def log_arguments_securely(args):
    args_log = copy.deepcopy(args)
    args_log.password = '<hidden>'
    logger.debug('Arguments: {}'.format(args_log))
    del args_log


if __name__ == '__main__':
    logger.debug('Start session {}'.format(datetime.now()))

    # FIXME remove that to allow standalone
    if os.getcwd() != os.path.dirname(os.path.abspath(__file__)):
        logger.debug('Wrong directory')
        print("Please change working directory to: %s" % os.path.dirname(os.path.abspath(__file__)))
        exit(1)

    args = parse_arguments()
    keyno = args.keyno
    passwd = None
    wait_e = args.wait_e

    log_arguments_securely(args)

    if args.password:
        passwd = args.password
    elif args.default_password:  # F for Factory setting
        passwd = DEFAULT_PW3
    if not passwd:
        try:
            passwd = getpass("Admin password: ")
        except:
            print('Quitting')
            exit(2)

    print('Firmware data to be used:')
    data = get_firmware_file(args.regnual, FirmwareType.REGNUAL)
    data_upgrade = get_firmware_file(args.gnuk, FirmwareType.GNUK)

    # Detect devices
    dev_strings = get_devices()
    if len(dev_strings) > 1:
        print('Only one device should be connected. Please remove other devices and retry.')
        exit(1)

    if dev_strings:
        print('Currently connected device strings:')
        print_device(dev_strings[0])
    else:
        print('Cannot identify device')

    logger.debug('Initial device strings: {}'.format(dev_strings))

    latest_tag = get_latest_release_data()

    print('Please note:')
    print('- Latest firmware available is: '
          '{} (published: {}),\n provided firmware: {}'
          .format(latest_tag['tag_name'], latest_tag['published_at'], args.gnuk))
    print('- All data will be removed from the device')
    print('- Do not interrupt the update process, or the device will not run properly')
    print('- Whole process should not take more than 1 minute')
    if args.yes:
        print('Accepted automatically')
    else:
        answer = input('Do you want to continue? [yes/no]: ')
        print('Entered: "{}"'.format(answer))
        logger.debug('Continue? "{}"'.format(answer))
        if answer != 'yes':
            print('Device is not modified. Exiting.')
            exit(1)

    update_done = False
    for attempt_counter in range(2):
        try:
            # First 4096-byte in data_upgrade is SYS, so, skip it.
            main(wait_e, keyno, passwd, data, data_upgrade[4096:], args.skip_bootloader)
            update_done = True
            break
        except ValueError as e:
            logger.exception('Error while running update')
            str_factory_reset = 'Please "factory-reset" your device to ' \
                                'continue (this will delete all user data from the device) ' \
                                'and try again with PIN="12345678".'
            if 'No ICC present' in str(e):
                kill_smartcard_services()
                # print('*** Please run update tool again.')
            else:
                print('*** Could not proceed with the update.')
                print('*** Found error: {}'.format(str(e)))
                # FIXME run factory reset here since data are lost anyway
                if str(e) == ERR_EMPTY_COUNTER:
                    print('*** Device returns "Attempt counter empty" error for Admin PIN.'
                          + ' ' + str_factory_reset
                          )
                if str(e) == ERR_INVALID_PIN:
                    print('*** Device returns "Invalid PIN" error.'
                          + ' ' + str_factory_reset)
                break
        except Exception as e:
            # unknown error, bail
            print('*** Found unexpected error: {}'.format(str(e)))
            break

    if not update_done:
        print()
        print('*** Could not proceed with the update. Please execute one or all of the following and try again:\n'
              '- reinsert device to the USB slot;\n'
              '- run factory-reset on the device;\n'
              '- close other applications, that possibly could use it (e.g. scdaemon, pcscd).\n')
        exit(1)

    dev_strings_upgraded = None
    takes_long_time = False
    print('Currently connected device strings (after upgrade):')
    for i in range(TIME_DETECT_DEVICE_AFTER_UPDATE_S):
        if i > TIME_DETECT_DEVICE_AFTER_UPDATE_LONG_S:
            if not takes_long_time:
                print('\n*** Please reinsert device to the USB slot')
                takes_long_time = True
        time.sleep(1)
        dev_strings_upgraded = get_devices()
        if len(dev_strings_upgraded) > 0:
            print()
            print_device(dev_strings_upgraded[0])
            break
        print('.', end='', flush=True)

    if not dev_strings_upgraded:
        print()
        print('Could not connect to the device. '
              'It should be working fine though after power cycle - please reinsert device to '
              'USB slot and test it.')
        print('Device could be removed from the USB slot.')
    logger.debug('Final device strings: {}'.format(dev_strings_upgraded))
    logger.debug('Finishing session {}'.format(datetime.now()))
