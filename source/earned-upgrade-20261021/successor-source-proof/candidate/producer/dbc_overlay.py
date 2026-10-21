"""Exact selected eight WDBC layouts and native pre-postprocessing SQL overlays.

Reuses effective-data-02 custody/error/interface. No network or acceptance minting.
Unknown column names must come from authenticated observed ordinal metadata.
"""
import ctypes
import errno
import hashlib
import platform
import struct
import sys
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from ingest_capture import CaptureError, decode, _digest, IDENTIFIER

MAP_SHA256 = '900120c3d6c3b4bc962459b54aa4d3150fe8c5c19881c15a5faaab251f0ed3f3'
_map_bytes = (Path(__file__).resolve().with_name('EIGHT-STORE-MAP.json')).read_bytes()
if hashlib.sha256(_map_bytes).hexdigest() != MAP_SHA256:
    raise CaptureError('eight-store source map custody')
CONTRACTS = {m['overlay_table']: dict(file=m['file'], format=m['format'],
    columns=tuple(c['column'] for c in m['known_dated_columns']),
    types=tuple(c['type'] for c in m['known_dated_columns']))
    for m in decode(_map_bytes)['mappings']}


def _hash(data, expected):
    if not isinstance(data, bytes) or hashlib.sha256(data).hexdigest() != _digest(expected):
        raise CaptureError('DBC/overlay hash')


def _native_uint(value):
    # Native Field::GetData<uint32> falls back to signed int32 for *_dbc tables.
    if value is None:
        return 0
    if type(value) is not int or not -(2**31) <= value <= 2**32-1:
        raise CaptureError('SQL uint32 field')
    return value & 0xffffffff


class _LongDouble(ctypes.c_longdouble):
    # A subclass prevents ctypes from narrowing the native return to Python float.
    pass


_libc = ctypes.CDLL(None, use_errno=True)
_strtold = _libc.strtold
_strtold.argtypes = (ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p))
_strtold.restype = _LongDouble
_libc.fegetround.restype = ctypes.c_int


def _float32(value):
    """Selected Linux StringTo<float>: std::stold then float; Field default is 1."""
    if value is None:
        return 0x3f800000
    if not isinstance(value, (str, int, Decimal)) or isinstance(value, bool):
        raise CaptureError('SQL float requires captured numeric text')
    if sys.platform != 'linux' or platform.machine() != 'x86_64' or sys.byteorder != 'little' or ctypes.sizeof(_LongDouble) != 16 or _libc.fegetround() != 0:
        raise CaptureError('selected Linux x87 long-double ABI/nearest rounding required')
    text = str(value).encode('utf-8')
    buffer = ctypes.create_string_buffer(text)
    end = ctypes.c_void_p()
    ctypes.set_errno(0)
    parsed = _strtold(buffer, ctypes.byref(end))
    # std::stold throws on no conversion/ERANGE; StringTo also rejects trailing text.
    if not text or end.value - ctypes.addressof(buffer) != len(text) or ctypes.get_errno() == errno.ERANGE:
        return 0x3f800000
    significand, sign_exponent = struct.unpack('<QH', bytes(parsed)[:10])
    sign = (sign_exponent >> 15) << 31
    exponent = sign_exponent & 0x7fff
    if exponent == 0x7fff:
        payload = significand & ((1 << 63) - 1)
        return sign | 0x7f800000 | (((payload >> 40) | 0x400000) if payload else 0)
    if significand == 0:
        return sign
    # Preserve all 64 significand bits through the second (binary32) rounding.
    fraction = Fraction(significand) * Fraction(2) ** ((exponent or 1) - 16383 - 63)
    n, d = fraction.numerator, fraction.denominator
    exponent = n.bit_length() - d.bit_length()
    if Fraction(2) ** exponent > fraction:
        exponent -= 1
    step = max(exponent - 23, -149)
    scaled = fraction / (Fraction(2) ** step)
    q, r = divmod(scaled.numerator, scaled.denominator)
    if 2*r > scaled.denominator or (2*r == scaled.denominator and q % 2):
        q += 1
    if q == 0:
        return sign
    if exponent < -126:
        return sign | q
    if q == 1 << 24:
        q >>= 1
        exponent += 1
    if exponent > 127:
        return sign | 0x7f800000
    return sign | ((exponent + 127) << 23) | (q - (1 << 23))


def _float_evidence(word, origin):
    binary = struct.pack('<I', word)
    return dict(float32_le_hex=binary.hex(), value_hex=struct.unpack('<f', binary)[0].hex(),
        bits_origin=origin, original_server_bits_verified=(origin == 'dbc'))


def validate_columns(table, columns, schema=None):
    contract = CONTRACTS[table]
    if not isinstance(columns, (list, tuple)) or len(columns) != len(contract['format']) or len(set(columns)) != len(columns):
        raise CaptureError('SQL DBC ordinal mismatch')
    if any(not isinstance(n, str) or IDENTIFIER.fullmatch(n) is None for n in columns) or columns[0] != 'ID':
        raise CaptureError('SQL DBC ordinal identifiers')
    if contract['columns'] and tuple(columns) != contract['columns']:
        raise CaptureError('SQL DBC ordinal mismatch')
    if schema is None:
        if not contract['columns']:
            raise CaptureError('observed SQL ordinal schema required')
        return
    if not isinstance(schema, list) or len(schema) != len(columns):
        raise CaptureError('SQL DBC schema width')
    integers = {'tinyint', 'smallint', 'mediumint', 'int', 'bigint'}
    for ordinal, (column, kind, meta) in enumerate(zip(columns, contract['format'], schema), 1):
        if not isinstance(meta, dict) or meta.get('TABLE_NAME', meta.get('table')) != table or meta.get('COLUMN_NAME', meta.get('column')) != column or meta.get('ORDINAL_POSITION', meta.get('ordinal')) != ordinal:
            raise CaptureError('SQL DBC observed ordinal mismatch')
        declared_type = meta.get('DATA_TYPE', meta.get('type', ''))
        if not isinstance(declared_type, str) or not declared_type.strip():
            raise CaptureError('SQL DBC observed field type')
        dtype = declared_type.lower().split('(')[0].split()[0]
        if kind in 'nib' and dtype not in integers or kind == 'f' and dtype not in {'float', 'double', 'decimal'} or kind == 's' and dtype not in {'char', 'varchar', 'text', 'tinytext', 'mediumtext', 'longtext'}:
            raise CaptureError('SQL DBC native field type mismatch')


def merge_dbc(table, dbc_bytes, expected_dbc_sha256, sql_bytes, expected_sql_sha256,
              sql_columns, sql_schema=None):
    if table not in CONTRACTS:
        raise CaptureError('unsupported fixed DBC')
    contract = CONTRACTS[table]
    fmt = contract['format']
    validate_columns(table, sql_columns, sql_schema)
    columns = tuple(sql_columns)
    _hash(dbc_bytes, expected_dbc_sha256)
    _hash(sql_bytes, expected_sql_sha256)
    if len(dbc_bytes) < 20:
        raise CaptureError('truncated WDBC')
    magic, count, fields, record_size, string_size = struct.unpack_from('<4s4I', dbc_bytes)
    expected_record_size = sum(1 if kind in 'bX' else 4 for kind in fmt)
    if magic != b'WDBC' or fields != len(fmt) or record_size != expected_record_size or len(dbc_bytes) != 20 + count*record_size + string_size:
        raise CaptureError('WDBC layout/bounds')
    strings = dbc_bytes[20 + count*record_size:]
    active, origins, evidence, field_origins = {}, {}, {}, {}
    duplicates = []
    for ordinal in range(count):
        row, floats, provenance = {}, {}, {}
        offset = 20 + ordinal*record_size
        for name, kind in zip(columns, fmt):
            word = struct.unpack_from('<B' if kind in 'bX' else '<I', dbc_bytes, offset)[0]
            offset += 1 if kind in 'bX' else 4
            if kind in 'xXd':
                continue
            if kind == 's':
                if word >= len(strings) or strings.find(b'\0', word) < 0:
                    raise CaptureError('WDBC string offset/termination')
                try:
                    row[name] = strings[word:strings.index(b'\0', word)].decode('utf-8')
                except UnicodeError as error:
                    raise CaptureError('WDBC string UTF-8 unsupported; retain original bytes') from error
            elif kind == 'f':
                row[name] = _float_evidence(word, 'dbc')
            else:
                row[name] = word
                if kind in 'ni':
                    floats[name] = word if word < 2**31 else word-2**32
            provenance[name] = 'dbc'
        identity = row['ID']
        if identity == 0xffffffff:
            raise CaptureError('native index uint32 overflow')
        if identity in active:
            duplicates.append(identity)
        active[identity], origins[identity], evidence[identity], field_origins[identity] = row, 'dbc', floats, provenance
    rows = [decode(line) for line in sql_bytes.splitlines() if line]
    seen, overridden = set(), []
    native_index_rows = max(active, default=0) + 1
    for source in rows:
        if not isinstance(source, dict) or set(source) != set(columns):
            raise CaptureError('SQL DBC row columns')
        if source['ID'] is None or (table in {'itemextendedcost_dbc', 'skilllineability_dbc'} and (type(source['ID']) is not int or not -(2**31) <= source['ID'] < 2**31)):
            raise CaptureError('SQL primary ID type/range')
        _native_uint(source['ID'])
    # The collector's order is irrelevant: native SELECT is ID DESC, not uint32 DESC.
    if rows:
        if any(source['ID'] is None for source in rows):
            raise CaptureError('SQL primary ID NULL')
        highest = _native_uint(max(source['ID'] for source in rows))
        if highest == 0xffffffff:
            raise CaptureError('native index uint32 overflow')
        native_index_rows = max(native_index_rows, highest+1)
    for source in rows:
        identity = _native_uint(source['ID'])
        if identity == 0xffffffff or identity >= native_index_rows:
            raise CaptureError('unsafe native SQL index allocation')
        if identity in seen:
            raise CaptureError('duplicate SQL primary ID')
        seen.add(identity)
        old = active.get(identity)
        row, signed, provenance = {}, {}, {}
        for name, kind in zip(columns, fmt):
            value = source[name]
            if kind in 'xXd':
                continue
            if kind == 's':
                if value is None:
                    value = ''
                if not isinstance(value, str) or '\0' in value:
                    raise CaptureError('SQL native string')
                if not value and old is not None:
                    row[name] = old[name]
                    provenance[name] = field_origins[identity][name]
                    continue
                row[name] = value
            elif kind == 'f':
                row[name] = _float_evidence(_float32(value), 'sql_decimal_reconstruction')
            elif kind == 'b':
                if value is None:
                    value = 0
                if type(value) is not int or not 0 <= value <= 255:
                    raise CaptureError('SQL uint8 field')
                row[name] = value
            else:
                row[name] = _native_uint(value)
                signed[name] = row[name] if row[name] < 2**31 else row[name]-2**32
            provenance[name] = 'sql_overlay'
        if old is not None:
            overridden.append(identity)
        active[identity], origins[identity], evidence[identity], field_origins[identity] = row, 'sql_overlay', signed, provenance
    unresolved = ['native_store_postprocessing_not_applied']
    if 's' in fmt:
        unresolved.append('localized_DBC_LoadStringsFrom_inputs_and_availableDbcLocales_not_captured')
    return dict(schema='cpi211-two-dbc-effective-fields-slice-v1' if table in {'itemextendedcost_dbc', 'skilllineability_dbc'} else 'cpi211-eight-dbc-effective-fields-slice-v1',
        effective_dataset_accepted=False, table=table, dbc_file=contract['file'], format=fmt,
        columns=list(columns), ignored_native_columns=[name for name, kind in zip(columns, fmt) if kind in 'xXd'],
        dbc_sha256=expected_dbc_sha256, sql_sha256=expected_sql_sha256, dbc_records=count, sql_records=len(rows),
        duplicate_base_ids_last_record_wins=sorted(set(duplicates)), overridden_base_ids=sorted(overridden),
        native_index_rows=native_index_rows, stage='base_plus_SQL_before_store_postprocessing', unresolved=unresolved,
        rows=[dict(native=active[k], origin=origins[k], field_origins=field_origins[k], signed_i32_interpretations=evidence[k]) for k in sorted(active)])
