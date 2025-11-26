import traceback
import uuid
import json
import datetime
from enum import Enum
from typing import Any, List, Dict


class ComplexEncoder(json.JSONEncoder):
    def default(self, obj: Any) -> Any:
        if hasattr(obj, "repr_json"):
            return obj.repr_json()
        elif hasattr(obj, "as_string"):
            return obj.as_string()
        elif isinstance(obj, uuid.UUID):
            return str(obj)
        elif isinstance(obj, datetime.datetime):
            return obj.isoformat()  # strftime("%Y-%m-%d %H:%M:%S %Z")
        elif isinstance(obj, datetime.date):
            return obj.strftime("%Y-%m-%d")
        elif isinstance(obj, datetime.timedelta):
            return str(obj)
        elif isinstance(obj, dict) or isinstance(obj, list):
            robj: str = get_pretty_dict_json_no_sort(obj)
            return robj
        else:
            return json.JSONEncoder.default(self, obj)


def print_pretty_dict_json(data: Any, indent: int = 4) -> None:
    from loguru import logger
    logger.info(json.dumps(data, indent=indent, sort_keys=True, cls=ComplexEncoder, default=str))


def get_pretty_dict_json(data: Any, indent: int = 4) -> str:
    return json.dumps(data, indent=indent, sort_keys=True, cls=ComplexEncoder, default=str)


def get_pretty_dict_json_no_sort(data: Any, indent: int = 4) -> str:
    return json.dumps(data, indent=indent, sort_keys=False, cls=ComplexEncoder, default=str)

def update_deep(base: Dict[str, Any]|List[Any], u: Dict[str, Any]|List[Any]) -> Dict[str, Any]|List[Any]:
    if isinstance(u, dict):
        if not isinstance(base, dict):
            base = {}

        for k, v in u.items():
            if isinstance(v, dict) or isinstance(v, list):
                base[k] = update_deep(base.get(k, {}), v)
            else:
                base[k] = v

    elif isinstance(u, list):
        if not isinstance(base, list):
            base = []  # may destroy the existing data if mismatch!!!

        # Stelle sicher, dass base lang genug ist
        # geht auch kompakter, aber so ist es gut lesbar
        while len(base) < len(u):
            base.append(None)

        # Stelle sicher, dass base nicht länger ist...
        # geht auch kompakter, aber so ist es gut lesbar
        while len(base) > len(u):
            base.pop()

        for i, v in enumerate(u):
            if isinstance(v, dict) or isinstance(v, list):
                base[i] = update_deep(base[i] if base[i] is not None else ({} if isinstance(v, dict) else []), v)  # type: ignore
            else:
                base[i] = v

    return base

def get_exception_tb_as_string(exc: Exception) -> str:
    tb1: traceback.TracebackException = traceback.TracebackException.from_exception(exc)
    tbsg = tb1.format()
    tbs = ""

    for line in tbsg:
        tbs = tbs + "\n" + line

    return tbs