from __future__ import annotations

from datetime import date, datetime, timedelta
import math
import random
from typing import Any, Callable

from .rule_eval.utils import CellError

XLS_FUNCTIONS = {
    "XLS_SUM",
    "XLS_IF",
    "XLS_MIN",
    "XLS_MAX",
    "XLS_AVG",
    "XLS_AVERAGE",
    "XLS_COUNT",
    "XLS_ABS",
    "XLS_ROUND",
    "XLS_VALUE",
    "XLS_YEAR",
    "XLS_EOMONTH",
    "XLS_NPV",
    "XLS_IRR",
    "XLS_INDEX",
    "XLS_MATCH",
    "XLS_XIRR",
    "XLS_TRUE",
    "XLS_FALSE",
    "XLS_OFFSET",
    "XLS_RAND",
    "XLS_RANDBETWEEN",
    "XLS_CHOOSE",
    "XLS_CONCATENATE",
    "XLS_DATE",
    "XLS_UPPER",
    "XLS_ROWS",
    "XLS_COLUMNS",
    "XLS_HLOOKUP",
    "XLS_VLOOKUP",
    "XLS_TEXT",
    "XLS_TODAY",
    "XLS_NOW",
    "XLS_REPT",
    "XLS_CODE",
    "XLS_CHAR",
}

# Legacy: kept for backwards compatibility, but code now uses isinstance(x, CellError)
# Kept as a tuple for any external code that may reference it
_ERROR_SENTINELS = ()  # Deprecated: use isinstance(x, CellError) instead
_EXCEL_EPOCH = date(1899, 12, 30)

# Derived constants — all traced to _EXCEL_EPOCH, not independently defined.
_EXCEL_EPOCH_SERIAL_1970 = (date(1970, 1, 1) - _EXCEL_EPOCH).days  # 25569
SECONDS_PER_DAY = 86_400.0
EXCEL_EPOCH_UNIX = -_EXCEL_EPOCH_SERIAL_1970 * SECONDS_PER_DAY  # -2209161600.0


def serial_days_to_unix_seconds(days: float) -> float:
    """Convert Excel day-serial to Unix timestamp (seconds since 1970-01-01 UTC).

    Boundary-only: for OS clock, API timestamps, logs, external integrations.
    Never called from the evaluation path.
    """
    return days * SECONDS_PER_DAY + EXCEL_EPOCH_UNIX


def unix_seconds_to_serial_days(seconds: float) -> float:
    """Convert Unix timestamp to Excel day-serial.

    Boundary-only: for OS clock, API timestamps, logs, external integrations.
    Never called from the evaluation path.
    """
    return (seconds - EXCEL_EPOCH_UNIX) / SECONDS_PER_DAY


def _split_text_format_sections(fmt: str) -> list[str]:
    if ";" in fmt:
        return [part.strip() for part in fmt.split(";")]
    # Some imported workbooks may use comma as a positive/negative section
    # delimiter for percent masks, e.g. "+0.0%,-0.0%".
    if fmt.count(",") == 1 and "%" in fmt:
        left, right = [part.strip() for part in fmt.split(",", 1)]
        if left and right and "%" in left and "%" in right:
            return [left, right]
    return [fmt]


def _format_number_with_mask(value: float, mask: str) -> str:
    placeholder_positions = [i for i, ch in enumerate(mask) if ch in ("0", "#")]
    if not placeholder_positions:
        return mask

    first = placeholder_positions[0]
    last = placeholder_positions[-1]
    prefix = mask[:first]
    body = mask[first : last + 1]
    suffix = mask[last + 1 :]

    percent_count = mask.count("%")
    scaled = float(value) * (100.0 ** percent_count)

    decimal_places = 0
    if "." in body:
        decimal_places = sum(1 for ch in body.split(".", 1)[1] if ch in ("0", "#"))

    integer_pattern = body.split(".", 1)[0]
    use_grouping = "," in integer_pattern
    fmt_spec = f",.{decimal_places}f" if use_grouping else f".{decimal_places}f"
    num_text = format(abs(scaled), fmt_spec)
    return f"{prefix}{num_text}{suffix}"


def _excel_datetime_from_serial(serial: float) -> datetime:
    day_count = int(math.floor(serial))
    fraction = float(serial) - float(day_count)
    base = datetime.combine(_EXCEL_EPOCH + timedelta(days=day_count), datetime.min.time())
    return base + timedelta(days=fraction)


def _looks_like_date_time_mask(mask: str) -> bool:
    text = mask.lower()
    return any(tok in text for tok in ("yyyy", "yy", "dd", "mm", "hh", "ss"))


def _format_excel_datetime_mask(value: float, mask: str) -> str:
    dt = _excel_datetime_from_serial(value)
    fmt = mask.lower()
    out: list[str] = []
    i = 0

    while i < len(fmt):
        if fmt.startswith("yyyy", i):
            out.append(f"{dt.year:04d}")
            i += 4
            continue
        if fmt.startswith("yy", i):
            out.append(f"{dt.year % 100:02d}")
            i += 2
            continue
        if fmt.startswith("dd", i):
            out.append(f"{dt.day:02d}")
            i += 2
            continue
        if fmt.startswith("d", i):
            out.append(str(dt.day))
            i += 1
            continue
        if fmt.startswith("hh", i):
            out.append(f"{dt.hour:02d}")
            i += 2
            continue
        if fmt.startswith("h", i):
            out.append(str(dt.hour))
            i += 1
            continue
        if fmt.startswith("ss", i):
            out.append(f"{dt.second:02d}")
            i += 2
            continue
        if fmt.startswith("s", i):
            out.append(str(dt.second))
            i += 1
            continue
        if fmt.startswith("mm", i):
            prev = fmt[i - 1] if i > 0 else ""
            nxt = fmt[i + 2] if i + 2 < len(fmt) else ""
            # Excel uses m/mm for either month or minute depending on context.
            # Treat m/mm adjacent to a time separator as minutes, otherwise month.
            is_minute = prev == ":" or nxt == ":"
            out.append(f"{dt.minute:02d}" if is_minute else f"{dt.month:02d}")
            i += 2
            continue
        if fmt.startswith("m", i):
            prev = fmt[i - 1] if i > 0 else ""
            nxt = fmt[i + 1] if i + 1 < len(fmt) else ""
            is_minute = prev == ":" or nxt == ":"
            out.append(str(dt.minute) if is_minute else str(dt.month))
            i += 1
            continue

        out.append(mask[i])
        i += 1

    return "".join(out)


def _xls_text(value: Any, fmt: Any) -> str:
    if value is None:
        return ""
    fmt_text = str(fmt)
    sections = _split_text_format_sections(fmt_text)

    try:
        num = float(value)
    except Exception:
        return str(value)

    if len(sections) >= 3 and num == 0:
        chosen = sections[2]
    elif len(sections) >= 2 and num < 0:
        chosen = sections[1]
    else:
        chosen = sections[0]

    if _looks_like_date_time_mask(chosen):
        return _format_excel_datetime_mask(num, chosen)

    return _format_number_with_mask(num, chosen)


def _excel_year_from_value(v: Any) -> float:
    if isinstance(v, datetime):
        return float(v.year)
    if isinstance(v, date):
        return float(v.year)

    if isinstance(v, str):
        text = v.strip()
        if not text:
            serial = 0.0
        else:
            try:
                serial = float(text)
            except ValueError:
                for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y"):
                    try:
                        return float(datetime.strptime(text, fmt).year)
                    except ValueError:
                        continue
                raise ValueError("xls_year requires a date-like argument")
    elif isinstance(v, (int, float, bool)):
        serial = float(v)
    elif v is None:
        serial = 0.0
    else:
        raise ValueError("xls_year requires a date-like argument")

    day_count = int(serial)
    return float((_EXCEL_EPOCH + timedelta(days=day_count)).year)


def _coerce_excel_date(v: Any, fn_name: str) -> date:
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v

    if isinstance(v, str):
        text = v.strip()
        if not text:
            serial = 0.0
        else:
            try:
                serial = float(text)
            except ValueError:
                for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%m/%d/%Y", "%d/%m/%Y"):
                    try:
                        return datetime.strptime(text, fmt).date()
                    except ValueError:
                        continue
                raise ValueError(f"{fn_name} requires a date-like argument")
    elif isinstance(v, (int, float, bool)):
        serial = float(v)
    elif v is None:
        serial = 0.0
    else:
        raise ValueError(f"{fn_name} requires a date-like argument")

    return _EXCEL_EPOCH + timedelta(days=int(serial))


def _excel_serial_from_date(d: date) -> float:
    return float((d - _EXCEL_EPOCH).days)


def _excel_serial_from_datetime(dt: datetime) -> float:
    base = datetime.combine(_EXCEL_EPOCH, datetime.min.time())
    delta = dt - base
    return delta.total_seconds() / 86400.0


def _xls_xirr(values: list[Any], dates: list[Any], guess: float = 0.1) -> float:
    if len(values) != len(dates) or not values:
        raise ValueError("xls_xirr requires equal non-empty values and dates arrays")

    cashflows = [float(v) for v in values]
    date_values = [_coerce_excel_date(d, "xls_xirr") for d in dates]

    has_pos = any(v > 0 for v in cashflows)
    has_neg = any(v < 0 for v in cashflows)
    if not (has_pos and has_neg):
        raise ValueError("xls_xirr requires at least one positive and one negative cash flow")

    t0 = date_values[0]
    year_fracs = [(d - t0).days / 365.0 for d in date_values]

    def f(rate: float) -> float:
        base = 1.0 + rate
        return sum(cf / (base ** t) for cf, t in zip(cashflows, year_fracs, strict=True))

    def df(rate: float) -> float:
        base = 1.0 + rate
        return sum((-t) * cf / (base ** (t + 1.0)) for cf, t in zip(cashflows, year_fracs, strict=True))

    r = float(guess)
    if r <= -0.999999:
        r = -0.9

    for _ in range(100):
        if r <= -0.999999:
            r = -0.999999
        fv = f(r)
        dv = df(r)
        if abs(dv) < 1e-12:
            break
        nxt = r - fv / dv
        if not math.isfinite(nxt):
            break
        if abs(nxt - r) <= 1e-10:
            return float(nxt)
        r = nxt

    raise ValueError("xls_xirr failed to converge")


def _xls_npv(rate: Any, values: list[Any]) -> float:
    r = float(rate)
    if r <= -1.0:
        raise ValueError("xls_npv requires rate > -1")
    cashflows = [float(v) for v in values]
    base = 1.0 + r
    return sum(cf / (base ** period) for period, cf in enumerate(cashflows, start=1))


def _xls_irr(values: list[Any], guess: float = 0.1) -> float:
    cashflows = [float(v) for v in values]
    if not cashflows:
        raise ValueError("xls_irr requires a non-empty values array")

    has_pos = any(v > 0 for v in cashflows)
    has_neg = any(v < 0 for v in cashflows)
    if not (has_pos and has_neg):
        raise ValueError("xls_irr requires at least one positive and one negative cash flow")

    def f(rate: float) -> float:
        base = 1.0 + rate
        return sum(cf / (base ** idx) for idx, cf in enumerate(cashflows))

    def df(rate: float) -> float:
        base = 1.0 + rate
        return sum((-idx) * cf / (base ** (idx + 1.0)) for idx, cf in enumerate(cashflows) if idx > 0)

    r = float(guess)
    if r <= -0.999999:
        r = -0.9

    for _ in range(100):
        if r <= -0.999999:
            r = -0.999999
        fv = f(r)
        dv = df(r)
        if abs(dv) < 1e-12:
            break
        nxt = r - fv / dv
        if not math.isfinite(nxt):
            break
        if abs(nxt - r) <= 1e-10:
            return float(nxt)
        r = nxt

    raise ValueError("xls_irr failed to converge")


def eval_xls_function(fn: str, args: list[Any], *, eval_node: Callable[[Any], Any]) -> Any:
    if fn == "XLS_IF":
        if len(args) != 3:
            raise ValueError("xls_if requires 3 arguments")
        cond = eval_node(args[0])
        if isinstance(cond, CellError):
            return cond
        return eval_node(args[1] if cond else args[2])

    if fn == "XLS_XIRR":
        if len(args) not in (2, 3):
            raise ValueError("xls_xirr requires 2 or 3 arguments")
        vals = [eval_node(a) for a in args]
        for v in vals:
            if isinstance(v, CellError):
                return v
        values = vals[0]
        dates = vals[1]
        guess = float(vals[2]) if len(vals) == 3 else 0.1
        if not isinstance(values, list) or not isinstance(dates, list):
            raise ValueError("xls_xirr requires array references for values and dates")
        return _xls_xirr(values, dates, guess)

    if fn == "XLS_NPV":
        if len(args) < 2:
            raise ValueError("xls_npv requires at least 2 arguments")
        vals = [eval_node(a) for a in args]
        for v in vals:
            if isinstance(v, CellError):
                return v
        rate = vals[0]
        if len(vals) == 2 and isinstance(vals[1], list):
            cashflows = vals[1]
        else:
            cashflows = vals[1:]
        return _xls_npv(rate, cashflows)

    if fn == "XLS_IRR":
        if len(args) not in (1, 2):
            raise ValueError("xls_irr requires 1 or 2 arguments")
        vals = [eval_node(a) for a in args]
        for v in vals:
            if isinstance(v, CellError):
                return v
        values = vals[0]
        if not isinstance(values, list):
            raise ValueError("xls_irr requires an array reference")
        guess = float(vals[1]) if len(vals) == 2 else 0.1
        return _xls_irr(values, guess)

    if fn == "XLS_CHOOSE":
        if len(args) < 2:
            raise ValueError("xls_choose requires at least 2 arguments")
        index_val = eval_node(args[0])
        if isinstance(index_val, CellError):
            return index_val
        index = int(float(index_val))
        if index < 1 or index >= len(args):
            raise ValueError("xls_choose index is out of range")
        return eval_node(args[index])

    if fn == "XLS_CONCATENATE":
        parts: list[str] = []
        for arg in args:
            v = eval_node(arg)
            if isinstance(v, CellError):
                return v
            parts.append("" if v is None else str(v))
        return "".join(parts)

    if fn == "XLS_DATE":
        if len(args) != 3:
            raise ValueError("xls_date requires 3 arguments")
        year_val = eval_node(args[0])
        month_val = eval_node(args[1])
        day_val = eval_node(args[2])
        for v in (year_val, month_val, day_val):
            if isinstance(v, CellError):
                return v
        dt = date(int(float(year_val)), int(float(month_val)), int(float(day_val)))
        return _excel_serial_from_date(dt)

    if fn == "XLS_UPPER":
        if len(args) != 1:
            raise ValueError("xls_upper requires 1 argument")
        v = eval_node(args[0])
        if isinstance(v, CellError):
            return v
        if v is None:
            return ""
        return str(v).upper()

    if fn == "XLS_TEXT":
        if len(args) != 2:
            raise ValueError("xls_text requires 2 arguments")
        v = eval_node(args[0])
        fmt = eval_node(args[1])
        if isinstance(v, CellError):
            return v
        if isinstance(fmt, CellError):
            return fmt
        return _xls_text(v, fmt)

    if fn == "XLS_TODAY":
        if args:
            raise ValueError("xls_today requires 0 arguments")
        return _excel_serial_from_date(date.today())

    if fn == "XLS_NOW":
        if args:
            raise ValueError("xls_now requires 0 arguments")
        return _excel_serial_from_datetime(datetime.now())

    # String/text functions that need string handling, not numeric conversion
    if fn == "XLS_REPT":
        if len(args) != 2:
            raise ValueError("xls_rept requires 2 arguments")
        text = eval_node(args[0])
        if isinstance(text, CellError):
            return text
        num_times = eval_node(args[1])
        if isinstance(num_times, CellError):
            return num_times
        text_str = str(text) if text is not None else ""
        repeat_count = int(float(num_times))
        if repeat_count < 0:
            raise ValueError("xls_rept repeat count must be >= 0")
        result = text_str * repeat_count
        # Enforce 1024 character limit
        if len(result) > 1024:
            result = result[:1024]
        return result

    if fn == "XLS_CODE":
        if len(args) != 1:
            raise ValueError("xls_code requires 1 argument")
        text = eval_node(args[0])
        if isinstance(text, CellError):
            return text
        text_str = str(text) if text is not None else ""
        if not text_str:
            # Return 0 for empty string (Excel compatibility)
            return 0.0
        return float(ord(text_str[0]))

    if fn == "XLS_CHAR":
        if len(args) != 1:
            raise ValueError("xls_char requires 1 argument")
        code_num = eval_node(args[0])
        if isinstance(code_num, CellError):
            return code_num
        code_val = int(float(code_num))
        # Excel char range: 1-255 (1 is special, 32-126 are printable)
        if code_val < 1 or code_val > 255:
            raise ValueError("xls_char code must be between 1 and 255")
        return chr(code_val)

    vals = [eval_node(a) for a in args]

    for v in vals:
        if isinstance(v, CellError):
            return v

    nums: list[float] = []
    for v in vals:
        if v is None:
            continue
        if isinstance(v, list):
            for inner in v:
                if inner is not None:
                    nums.append(float(inner))
        else:
            nums.append(float(v))

    if fn == "XLS_ABS":
        if len(vals) != 1:
            raise ValueError("xls_abs requires 1 argument")
        return abs(vals[0])

    if fn == "XLS_ROUND":
        if len(vals) not in (1, 2):
            raise ValueError("xls_round requires 1 or 2 arguments")
        places = int(vals[1]) if len(vals) == 2 else 0
        return round(vals[0], places)

    if fn == "XLS_VALUE":
        if len(vals) != 1:
            raise ValueError("xls_value requires 1 argument")
        v = vals[0]
        if v is None:
            return 0.0
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            text = v.strip()
            if not text:
                return 0.0
            return float(text)
        raise ValueError(f"xls_value cannot convert {type(v).__name__}")

    if fn == "XLS_YEAR":
        if len(vals) != 1:
            raise ValueError("xls_year requires 1 argument")
        return _excel_year_from_value(vals[0])

    if fn == "XLS_EOMONTH":
        if len(vals) != 2:
            raise ValueError("xls_eomonth requires 2 arguments")
        start_date = _coerce_excel_date(vals[0], "xls_eomonth")
        month_delta = int(float(vals[1]))

        month_index = start_date.year * 12 + (start_date.month - 1) + month_delta
        target_year = month_index // 12
        target_month = month_index % 12 + 1
        if target_month == 12:
            next_month = date(target_year + 1, 1, 1)
        else:
            next_month = date(target_year, target_month + 1, 1)
        end_of_month = next_month - timedelta(days=1)
        return _excel_serial_from_date(end_of_month)

    if fn == "XLS_TRUE":
        if vals:
            raise ValueError("xls_true requires 0 arguments")
        return 1.0

    if fn == "XLS_FALSE":
        if vals:
            raise ValueError("xls_false requires 0 arguments")
        return 0.0

    if fn == "XLS_RAND":
        if vals:
            raise ValueError("xls_rand requires 0 arguments")
        return random.random()

    if fn == "XLS_RANDBETWEEN":
        if len(vals) != 2:
            raise ValueError("xls_randbetween requires 2 arguments")
        low_int = int(float(vals[0]))
        high_int = int(float(vals[1]))
        if low_int > high_int:
            raise ValueError("xls_randbetween requires bottom <= top")
        return float(random.randint(low_int, high_int))

    if fn == "XLS_SUM":
        return sum(nums)
    if fn == "XLS_MIN":
        return min(nums) if nums else 0.0
    if fn == "XLS_MAX":
        return max(nums) if nums else 0.0
    if fn in ("XLS_AVG", "XLS_AVERAGE"):
        return sum(nums) / len(nums) if nums else 0.0
    if fn == "XLS_COUNT":
        return float(len(nums))

    raise ValueError(f"Unknown xls_* function {fn!r}")
