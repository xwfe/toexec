"""读采集日志，按标定值换算。标定值不在这里，在日志表头。"""


def parse_line(line):
    ts, raw = line.split(",", 1)
    return int(ts), int(raw)


def to_celsius(raw, calibration_offset):
    return (raw - calibration_offset) / 1000.0


def read_all(path, calibration_offset):
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            ts, raw = parse_line(line)
            out.append((ts, to_celsius(raw, calibration_offset)))
    return out
