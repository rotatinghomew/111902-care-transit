"""时刻与窗口工具。

时刻均为当地时间，字符串格式 "HH:MM"；窗口为 "HH:MM-HH:MM"。
约定：上下车时刻落在窗口端点上也算符合（闭区间）；换乘接续要求
后段发车时刻严格晚于到达时刻（零缓冲不算可接续），同 run 内 seq
相邻的续行按班表本身的连续开行处理，不做步行接续判定。
"""


def to_minutes(text):
    hour, minute = text.split(":")
    return int(hour) * 60 + int(minute)


def in_window(moment, window):
    start_text, end_text = window.split("-")
    return to_minutes(start_text) <= to_minutes(moment) <= to_minutes(end_text)


def overlap(start_a, end_a, start_b, end_b):
    """半开区间是否占用重合时间；端点相接（end == start）不算重合。"""
    return start_a < end_b and start_b < end_a


def hhmm(minutes):
    return f"{minutes // 60:02d}:{minutes % 60:02d}"
