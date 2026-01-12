#!/usr/bin/env python3
"""
信号验证脚本 - 分析已记录的插针信号数据

功能：
1. 加载历史信号数据
2. 模拟交易并计算盈亏
3. 生成多时间段对比报告
4. 输出最佳持仓时间建议
"""

import os
import sys
from datetime import datetime

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.data import SignalRecorder
from src.backtest import BatchSimulator
from src.analysis import SignalAnalytics, ReportGenerator


# ============== 配置 ==============

VALIDATION_CONFIG = {
    "position_size_usd": 15,
    "leverage": 20,
    "hold_periods": [30, 60, 90, 180],
}

RECORDER_CONFIG = {
    "data_dir": "data",
    "signal_file_prefix": "pin_signals_",
}


# ============== 主函数 ==============

def main():
    print("=" * 70)
    print("              插针信号验证分析")
    print(f"              {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 70)

    # 创建记录器
    recorder = SignalRecorder(RECORDER_CONFIG)

    # 显示可用数据文件
    print("\n📁 可用的数据文件:")
    data_dir = RECORDER_CONFIG["data_dir"]
    prefix = RECORDER_CONFIG["signal_file_prefix"]

    if not os.path.exists(data_dir):
        print(f"   数据目录不存在: {data_dir}")
        print("   请先运行 test_pin_detector.py 收集信号数据")
        return

    files = [f for f in os.listdir(data_dir) if f.startswith(prefix) and f.endswith('.json')]

    if not files:
        print(f"   暂无数据文件")
        print("   请先运行 test_pin_detector.py 收集信号数据")
        return

    for f in sorted(files, reverse=True):
        file_path = os.path.join(data_dir, f)
        # 计算记录数
        with open(file_path, 'r') as fp:
            count = sum(1 for _ in fp)
        print(f"   {f}: {count} 条记录")

    # 选择数据源
    print("\n选择分析范围:")
    print("1. 所有数据")
    print("2. 今天的数据")
    print("3. 指定日期 (YYYYMMDD)")

    choice = input("\n请选择 (回车默认=1): ").strip()

    if choice == "2":
        date = datetime.now().strftime("%Y%m%d")
        records = recorder.load_records(date)
    elif choice and choice != "1":
        date = choice
        records = recorder.load_records(date)
    else:
        records = recorder.get_all_records()

    print(f"\n📊 加载了 {len(records)} 条信号记录")

    if not records:
        print("没有可用的信号数据")
        return

    # 显示信号概览
    print("\n📋 信号概览:")
    symbol_count = {}
    direction_count = {"UP": 0, "DOWN": 0}

    for r in records:
        symbol_count[r.symbol] = symbol_count.get(r.symbol, 0) + 1
        if r.direction in direction_count:
            direction_count[r.direction] += 1

    print(f"   交易对分布:")
    for symbol, count in sorted(symbol_count.items(), key=lambda x: -x[1]):
        print(f"     {symbol}: {count}")

    print(f"   方向分布:")
    for direction, count in direction_count.items():
        dir_name = "做多 (UP)" if direction == "UP" else "做空 (DOWN)"
        print(f"     {dir_name}: {count}")

    # 模拟交易
    print("\n⚙️  正在模拟交易...")
    simulator = BatchSimulator(VALIDATION_CONFIG)
    records = simulator.simulate_and_update(records)

    # 生成报告
    print("\n📈 正在生成分析报告...")
    analytics = SignalAnalytics(VALIDATION_CONFIG)
    report = analytics.analyze(records)

    # 打印报告
    generator = ReportGenerator()
    generator.print_report(report)

    # 保存详细数据
    save = input("\n💾 是否保存详细数据到CSV? (y/n): ").strip().lower()
    if save == 'y':
        save_to_csv(records, VALIDATION_CONFIG)


def save_to_csv(records, config):
    """保存详细数据到CSV文件"""
    import csv

    filename = f"data/validation_results_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    os.makedirs("data", exist_ok=True)

    with open(filename, 'w', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        writer.writerow([
            'ID', 'Symbol', 'Direction', 'DetectedAt',
            'StartPrice', 'PeakPrice', 'CurrentPrice',
            'Amplitude%', 'Retracement%', 'Duration_ms',
            'PriceBefore30s', 'PriceBefore60s', 'PriceBefore90s', 'PriceBefore180s',
            'PriceAfter30s', 'PriceAfter60s', 'PriceAfter90s', 'PriceAfter180s',
            'Profit30s_USD', 'Profit30s_%',
            'Profit60s_USD', 'Profit60s_%',
            'Profit90s_USD', 'Profit90s_%',
            'Profit180s_USD', 'Profit180s_%',
        ])

        for r in records:
            writer.writerow([
                r.id[:8], r.symbol, r.direction,
                r.detected_at.strftime('%Y-%m-%d %H:%M:%S') if r.detected_at else '',
                f'{r.start_price:.6f}', f'{r.peak_price:.6f}', f'{r.current_price:.6f}',
                f'{r.amplitude_percent:.2f}', f'{r.retracement_percent:.2f}', r.duration_ms,
                f'{r.price_before_30s:.6f}' if r.price_before_30s else '',
                f'{r.price_before_60s:.6f}' if r.price_before_60s else '',
                f'{r.price_before_90s:.6f}' if r.price_before_90s else '',
                f'{r.price_before_180s:.6f}' if r.price_before_180s else '',
                f'{r.price_after_30s:.6f}' if r.price_after_30s else '',
                f'{r.price_after_60s:.6f}' if r.price_after_60s else '',
                f'{r.price_after_90s:.6f}' if r.price_after_90s else '',
                f'{r.price_after_180s:.6f}' if r.price_after_180s else '',
                f'{r.profit_30s_usd:.2f}' if r.profit_30s_usd is not None else '',
                f'{r.profit_30s_percent:.2f}' if r.profit_30s_percent is not None else '',
                f'{r.profit_60s_usd:.2f}' if r.profit_60s_usd is not None else '',
                f'{r.profit_60s_percent:.2f}' if r.profit_60s_percent is not None else '',
                f'{r.profit_90s_usd:.2f}' if r.profit_90s_usd is not None else '',
                f'{r.profit_90s_percent:.2f}' if r.profit_90s_percent is not None else '',
                f'{r.profit_180s_usd:.2f}' if r.profit_180s_usd is not None else '',
                f'{r.profit_180s_percent:.2f}' if r.profit_180s_percent is not None else '',
            ])

    print(f"✅ 已保存到: {filename}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n👋 用户中断")
    except Exception as e:
        print(f"\n❌ 错误: {e}")
        import traceback
        traceback.print_exc()
