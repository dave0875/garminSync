#!/usr/bin/env python3
"""Read-only metadata and integrity diagnostics using Garmin's official FIT SDK.

Exit 0: all files decode and pass integrity. Exit 1: at least one warning.
Warnings never determine whether a native file belongs in the raw backup.
"""
import argparse
from datetime import datetime
from importlib.metadata import version
import json
from pathlib import Path
import sys

from garmin_fit_sdk import Decoder, Stream


def inspect_file(filename):
    result = {
        'file': str(filename), 'sdk_version': version('garmin-fit-sdk'),
        'interpreter': sys.executable, 'is_fit': False, 'integrity_ok': False,
        'decode_ok': False, 'file_type': None, 'timestamps': [],
        'message_counts': {}, 'record_count': 0, 'session_count': 0,
        'activity_count': 0, 'unknown_message_types': [], 'errors': [],
    }
    try:
        data = Path(filename).read_bytes()
        result['bytes'] = len(data)
        stream = Stream.from_byte_array(bytearray(data))
        try:
            result['is_fit'] = Decoder(stream).is_fit()
            stream.reset()
            result['integrity_ok'] = Decoder(stream).check_integrity()
        finally:
            stream.close()
        # A fresh stream avoids depending on decoder cursor side effects.
        stream = Stream.from_byte_array(bytearray(data))
        try:
            messages, errors = Decoder(stream).read()
        finally:
            stream.close()
        result['errors'] = [str(error) for error in errors]
        result['decode_ok'] = result['is_fit'] and not errors
        result['message_counts'] = {str(name): len(rows) for name, rows in messages.items()}
        result['unknown_message_types'] = [
            str(k) for k in messages
            if isinstance(k, int) or (isinstance(k, str) and k.isdecimal())
        ]
        ids = messages.get('file_id_mesgs', [])
        if ids:
            result['file_type'] = ids[0].get('type')
        result['record_count'] = len(messages.get('record_mesgs', []))
        result['session_count'] = len(messages.get('session_mesgs', []))
        result['activity_count'] = len(messages.get('activity_mesgs', []))
        timestamps = set()
        for rows in messages.values():
            for row in rows:
                for key in ('timestamp', 'time_created', 'start_time'):
                    value = row.get(key)
                    if isinstance(value, datetime):
                        timestamps.add(value.isoformat())
        if timestamps:
            ordered = sorted(timestamps)
            result['timestamps'] = [ordered[0], ordered[-1]]
        if not result['integrity_ok']:
            result['errors'].append('SDK integrity check failed; preserve original bytes')
    except Exception as error:
        result['errors'].append(f'{type(error).__name__}: {error}')
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('files', nargs='+', type=Path)
    args = parser.parse_args()
    results = [inspect_file(path) for path in args.files]
    print(json.dumps(results, indent=2, default=str))
    return 0 if all(r['decode_ok'] and r['integrity_ok'] for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
