"""Accepted raw JSON/integer custody primitives; production uses sealed descriptors.

Exact selected helper bytes retained from continuation07; no old capture quotas.
"""
import json
import re
from decimal import Decimal
IDENTIFIER = re.compile(r'[A-Za-z_][A-Za-z0-9_]*\Z')

class CaptureError(ValueError): pass

def _pairs(rows):
    result={}
    for key,value in rows:
        if key in result: raise CaptureError('duplicate JSON key')
        result[key]=value
    return result

def decode(data):
    try:
        return json.loads(data,object_pairs_hook=_pairs,
            parse_float=Decimal,parse_constant=lambda _: (_ for _ in ()).throw(CaptureError('nonfinite JSON')))
    except (ValueError,UnicodeError) as error: raise CaptureError('invalid capture JSON') from error

def _uint(value,maximum=2**32-1):
    if type(value) is not int or not 0<=value<=maximum: raise CaptureError('unsigned integer')
    return value

def _digest(value):
    if not isinstance(value,str) or re.fullmatch('[0-9a-f]{64}',value) is None: raise CaptureError('SHA256')
    return value
