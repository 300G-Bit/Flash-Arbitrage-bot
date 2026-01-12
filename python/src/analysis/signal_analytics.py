"""
Signal Analytics Module.

统计分析插针信号的盈利能力，生成多时间段对比报告。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from statistics import mean, stdev
import sys

import structlog

from ..data.signal_recorder import PinSignalRecord
from ..backtest.trade_simulator import SimulationResult, TradeSimulator

logger = structlog.get_logger(__name__)


# ============== 配置 ==============

ANALYTICS_CONFIG = {
    "position_size_usd": 15,
    "leverage": 20,
    "hold_periods": [30, 60, 90, 180],
}


# ============== 数据结构 ==============

@dataclass
class PeriodStats:
    """单个时间段的统计"""
    hold_period: int

    total_signals: int = 0
    profitable_signals: int = 0
    losing_signals: int = 0

    total_profit_usd: float = 0
    total_loss_usd: float = 0

    avg_profit_usd: float = 0
    avg_loss_usd: float = 0
    avg_pnl_usd: float = 0

    avg_profit_percent: float = 0
    avg_loss_percent: float = 0
    avg_pnl_percent: float = 0

    win_rate: float = 0
    profit_factor: float = 0  # 盈亏比

    max_profit_usd: float = 0
    max_loss_usd: float = 0

    max_consecutive_wins: int = 0
    max_consecutive_losses: int = 0

    sharpe_ratio: float = 0  # 夏普比率
    sortino_ratio: float = 0  # 索提诺比率

    def __str__(self) -> str:
        return (
            f"持仓{self.hold_period}秒: "
            f"平均{self.avg_pnl_usd:+.2f} USDT ({self.avg_pnl_percent:+.1f}%)   "
            f"盈亏比: {self.profit_factor:.1f}:1   "
            f"胜率: {self.win_rate:.1f}%"
        )


@dataclass
class DirectionStats:
    """方向统计"""
    direction: str  # UP/DOWN

    total_signals: int = 0
    tradeable_signals: int = 0
    tradeable_rate: float = 0

    avg_profit_usd: float = 0
    avg_profit_percent: float = 0


@dataclass
class AnalyticsReport:
    """分析报告"""
    total_signals: int
    tradeable_signals: int
    tradeable_rate: float

    period_stats: Dict[int, PeriodStats]  # {hold_period: PeriodStats}
    direction_stats: Dict[str, DirectionStats]  # {direction: DirectionStats}

    best_period: int  # 最佳持仓时间
    best_profit_usd: float
    best_profit_percent: float

    # 总体盈亏（以最佳持仓时间计算）
    total_profit_usd: float
    total_loss_usd: float
    net_profit_usd: float
    net_profit_percent: float

    max_drawdown_usd: float

    start_time: datetime = None
    end_time: datetime = None


# ============== 统计分析器 ==============

class SignalAnalytics:
    """
    信号统计分析器

    功能：
    1. 统计各时间段的盈亏
    2. 对比不同持仓时间的效果
    3. 找出最佳持仓时间
    4. 生成分析报告
    """

    def __init__(self, config: Optional[Dict] = None):
        """初始化分析器

        Args:
            config: 配置字典
        """
        self.config = {**ANALYTICS_CONFIG, **(config or {})}
        self.simulator = TradeSimulator(config)
        self.logger = logger.bind(component="SignalAnalytics")

    def analyze(self, records: List[PinSignalRecord]) -> AnalyticsReport:
        """分析信号记录

        Args:
            records: 信号记录列表

        Returns:
            AnalyticsReport: 分析报告
        """
        if not records:
            return self._empty_report()

        self.logger.info(f"Analyzing {len(records)} signals")

        # 模拟所有信号
        simulation_results = []
        for record in records:
            try:
                result = self.simulator.simulate(record)
                simulation_results.append(result)
            except Exception as e:
                self.logger.error(
                    "Simulation failed",
                    record_id=record.id[:8],
                    error=str(e)
                )

        # 计算各时间段统计
        period_stats = self._calculate_period_stats(records, simulation_results)

        # 计算方向统计
        direction_stats = self._calculate_direction_stats(simulation_results)

        # 找出最佳持仓时间
        best_period, best_profit_usd, best_profit_percent = self._find_best_period(
            period_stats
        )

        # 计算总体盈亏
        total_profit, total_loss, net_profit, net_profit_pct = self._calculate_total_pnl(
            simulation_results, best_period
        )

        # 计算最大回撤
        max_drawdown = self._calculate_max_drawdown(
            records, simulation_results, best_period
        )

        # 时间范围
        start_time = min(r.detected_at for r in records) if records else None
        end_time = max(r.detected_at for r in records) if records else None

        # 可交易信号数
        tradeable_count = sum(1 for r in simulation_results if r.is_tradeable)
        tradeable_rate = (tradeable_count / len(records) * 100) if records else 0

        return AnalyticsReport(
            total_signals=len(records),
            tradeable_signals=tradeable_count,
            tradeable_rate=tradeable_rate,
            period_stats=period_stats,
            direction_stats=direction_stats,
            best_period=best_period,
            best_profit_usd=best_profit_usd,
            best_profit_percent=best_profit_percent,
            total_profit_usd=total_profit,
            total_loss_usd=total_loss,
            net_profit_usd=net_profit,
            net_profit_percent=net_profit_pct,
            max_drawdown_usd=max_drawdown,
            start_time=start_time,
            end_time=end_time,
        )

    def _calculate_period_stats(
        self,
        records: List[PinSignalRecord],
        simulation_results: List[SimulationResult]
    ) -> Dict[int, PeriodStats]:
        """计算各时间段统计"""
        period_stats = {}

        for hold_period in self.config["hold_periods"]:
            profits = []
            losses = []
            all_pnls = []

            profitable_count = 0
            total_profit = 0
            total_loss = 0

            max_profit = 0
            max_loss = 0

            # 收集数据
            for result in simulation_results:
                if hold_period not in result.results:
                    continue

                trade = result.results[hold_period]
                pnl = trade.profit_usd
                all_pnls.append(pnl)

                if pnl > 0:
                    profitable_count += 1
                    profits.append(pnl)
                    total_profit += pnl
                    max_profit = max(max_profit, pnl)
                else:
                    losses.append(abs(pnl))
                    total_loss += abs(pnl)
                    max_loss = max(max_loss, abs(pnl))

            total_count = len(all_pnls)

            if total_count == 0:
                continue

            # 计算统计值
            avg_profit = mean(profits) if profits else 0
            avg_loss = mean(losses) if losses else 0
            avg_pnl = mean(all_pnls)

            # 计算百分比
            position_size = self.config["position_size_usd"]
            avg_profit_pct = (avg_profit / position_size * 100) if avg_profit else 0
            avg_loss_pct = (avg_loss / position_size * 100) if avg_loss else 0
            avg_pnl_pct = (avg_pnl / position_size * 100)

            win_rate = (profitable_count / total_count * 100)

            # 盈亏比
            profit_factor = (total_profit / total_loss) if total_loss > 0 else 0

            # 计算连续盈亏
            consecutive_wins, consecutive_losses = self._calculate_streaks(all_pnls)

            # 夏普比率
            sharpe = self._calculate_sharpe(all_pnls)

            # 索提诺比率
            sortino = self._calculate_sortino(all_pnls)

            period_stats[hold_period] = PeriodStats(
                hold_period=hold_period,
                total_signals=total_count,
                profitable_signals=profitable_count,
                losing_signals=total_count - profitable_count,
                total_profit_usd=total_profit,
                total_loss_usd=total_loss,
                avg_profit_usd=avg_profit,
                avg_loss_usd=avg_loss,
                avg_pnl_usd=avg_pnl,
                avg_profit_percent=avg_profit_pct,
                avg_loss_percent=avg_loss_pct,
                avg_pnl_percent=avg_pnl_pct,
                win_rate=win_rate,
                profit_factor=profit_factor,
                max_profit_usd=max_profit,
                max_loss_usd=max_loss,
                max_consecutive_wins=consecutive_wins,
                max_consecutive_losses=consecutive_losses,
                sharpe_ratio=sharpe,
                sortino_ratio=sortino,
            )

        return period_stats

    def _calculate_direction_stats(
        self,
        simulation_results: List[SimulationResult]
    ) -> Dict[str, DirectionStats]:
        """计算方向统计"""
        direction_stats = {}

        for direction in ["UP", "DOWN"]:
            # 筛选该方向的信号
            results = [r for r in simulation_results if r.direction == direction]

            if not results:
                continue

            # 可交易信号数
            tradeable = sum(1 for r in results if r.is_tradeable)

            # 平均盈利（以最佳时间段）
            profits = []
            for r in results:
                if r.is_tradeable and r.best_profit_usd is not None:
                    profits.append(r.best_profit_usd)

            avg_profit = mean(profits) if profits else 0
            avg_profit_pct = (avg_profit / self.config["position_size_usd"] * 100
                            if avg_profit else 0)

            direction_stats[direction] = DirectionStats(
                direction=direction,
                total_signals=len(results),
                tradeable_signals=tradeable,
                tradeable_rate=(tradeable / len(results) * 100) if results else 0,
                avg_profit_usd=avg_profit,
                avg_profit_percent=avg_profit_pct,
            )

        return direction_stats

    def _find_best_period(
        self,
        period_stats: Dict[int, PeriodStats]
    ) -> Tuple[int, float, float]:
        """找出最佳持仓时间"""
        best_period = None
        best_profit = float('-inf')
        best_profit_pct = 0

        for period, stats in period_stats.items():
            if stats.avg_pnl_usd > best_profit:
                best_profit = stats.avg_pnl_usd
                best_period = period
                best_profit_pct = stats.avg_pnl_percent

        if best_period is None:
            best_period = self.config["hold_periods"][0]

        return best_period, best_profit, best_profit_pct

    def _calculate_total_pnl(
        self,
        simulation_results: List[SimulationResult],
        best_period: int
    ) -> Tuple[float, float, float, float]:
        """计算总体盈亏"""
        profits = []
        losses = []

        for result in simulation_results:
            if best_period in result.results:
                pnl = result.results[best_period].profit_usd
                if pnl > 0:
                    profits.append(pnl)
                else:
                    losses.append(abs(pnl))

        total_profit = sum(profits)
        total_loss = sum(losses)
        net_profit = total_profit - total_loss

        position_size = self.config["position_size_usd"]
        net_profit_pct = (net_profit / position_size * 100)

        return total_profit, total_loss, net_profit, net_profit_pct

    def _calculate_max_drawdown(
        self,
        records: List[PinSignalRecord],
        simulation_results: List[SimulationResult],
        best_period: int
    ) -> float:
        """计算最大回撤"""
        pnls = []

        for result in simulation_results:
            if best_period in result.results:
                pnls.append(result.results[best_period].profit_usd)

        if not pnls:
            return 0

        max_drawdown = 0
        peak = 0
        cumulative = 0

        for pnl in pnls:
            cumulative += pnl
            peak = max(peak, cumulative)
            drawdown = peak - cumulative
            max_drawdown = max(max_drawdown, drawdown)

        return max_drawdown

    def _calculate_streaks(self, pnls: List[float]) -> Tuple[int, int]:
        """计算最大连续盈亏"""
        max_win_streak = 0
        max_loss_streak = 0

        current_win_streak = 0
        current_loss_streak = 0

        for pnl in pnls:
            if pnl > 0:
                current_win_streak += 1
                current_loss_streak = 0
                max_win_streak = max(max_win_streak, current_win_streak)
            else:
                current_loss_streak += 1
                current_win_streak = 0
                max_loss_streak = max(max_loss_streak, current_loss_streak)

        return max_win_streak, max_loss_streak

    def _calculate_sharpe(self, pnls: List[float]) -> float:
        """计算夏普比率"""
        if len(pnls) < 2:
            return 0

        avg_pnl = mean(pnls)
        std_pnl = stdev(pnls) if len(pnls) > 1 else 0

        if std_pnl == 0:
            return 0

        # 假设无风险利率为0
        return (avg_pnl / std_pnl) * 100  # 转换为更易读的数值

    def _calculate_sortino(self, pnls: List[float]) -> float:
        """计算索提诺比率"""
        if len(pnls) < 2:
            return 0

        avg_pnl = mean(pnls)

        # 只计算亏损的标准差
        losses = [p for p in pnls if p < 0]
        if len(losses) < 2:
            return 0

        std_loss = stdev(losses)

        if std_loss == 0:
            return 0

        return (avg_pnl / std_loss) * 100

    def _empty_report(self) -> AnalyticsReport:
        """返回空报告"""
        return AnalyticsReport(
            total_signals=0,
            tradeable_signals=0,
            tradeable_rate=0,
            period_stats={},
            direction_stats={},
            best_period=60,
            best_profit_usd=0,
            best_profit_percent=0,
            total_profit_usd=0,
            total_loss_usd=0,
            net_profit_usd=0,
            net_profit_percent=0,
            max_drawdown_usd=0,
        )


# ============== 报告生成器 ==============

class ReportGenerator:
    """报告生成器"""

    def __init__(self):
        self.logger = logger.bind(component="ReportGenerator")

    def generate_text_report(self, report: AnalyticsReport) -> str:
        """生成文本报告

        Args:
            report: 分析报告

        Returns:
            报告文本
        """
        lines = []

        # 标题
        lines.append("=" * 70)
        lines.append("                插针信号验证报告")
        lines.append(f"本金: {ANALYTICS_CONFIG['position_size_usd']} USDT, "
                    f"杠杆: {ANALYTICS_CONFIG['leverage']}x")

        if report.start_time and report.end_time:
            lines.append(f"统计周期: {report.start_time.strftime('%Y-%m-%d %H:%M')} - "
                        f"{report.end_time.strftime('%Y-%m-%d %H:%M')}")

        lines.append("=" * 70)
        lines.append("")

        # 总体统计
        lines.append("-" * 70)
        lines.append("总体统计")
        lines.append("-" * 70)
        lines.append(f"  总信号数:        {report.total_signals}")
        lines.append(f"  可操作信号:      {report.tradeable_signals}  "
                    f"({report.tradeable_rate:.1f}%)")
        lines.append(f"  不可操作信号:    {report.total_signals - report.tradeable_signals}  "
                    f"({100 - report.tradeable_rate:.1f}%)")
        lines.append("")

        # 各时间段对比
        lines.append("-" * 70)
        lines.append("各持仓时间段盈利对比")
        lines.append("-" * 70)

        best_marker = ""
        for period, stats in sorted(report.period_stats.items()):
            if period == report.best_period:
                best_marker = "  ⭐最佳"
            else:
                best_marker = ""

            lines.append(
                f"  持仓{period}秒:  "
                f"平均{stats.avg_pnl_usd:+.2f} USDT  ({stats.avg_pnl_percent:+.1f}%)   "
                f"盈亏比: {stats.profit_factor:.1f}:1   "
                f"胜率: {stats.win_rate:.1f}%"
                + best_marker
            )

        if report.period_stats:
            lines.append("")
            best_stats = report.period_stats.get(report.best_period)
            if best_stats:
                lines.append(f"  结论: {report.best_period}秒持仓时间收益最佳")

        lines.append("")

        # 分方向统计
        if report.direction_stats:
            lines.append("-" * 70)
            lines.append("分方向统计")
            lines.append("-" * 70)

            for direction, stats in report.direction_stats.items():
                direction_name = "做多 (UP)" if direction == "UP" else "做空 (DOWN)"
                lines.append(
                    f"  {direction_name}:   "
                    f"{stats.total_signals}信号 → {stats.tradeable_signals}可操作 "
                    f"({stats.tradeable_rate:.0f}%)   "
                    f"平均{stats.avg_profit_usd:+.2f} USDT"
                )
            lines.append("")

        # 总体盈亏
        lines.append("-" * 70)
        lines.append(f"总体盈亏 ({report.best_period}秒持仓)")
        lines.append("-" * 70)
        lines.append(f"  总盈利:  +{report.total_profit_usd:.2f} USDT")
        lines.append(f"  总亏损:  -{report.total_loss_usd:.2f} USDT")
        lines.append(f"  净盈利:  {report.net_profit_usd:+.2f} USDT "
                    f"({report.net_profit_percent:+.1f}%)")
        lines.append(f"  最大回撤: -{report.max_drawdown_usd:.2f} USDT")

        best_stats = report.period_stats.get(report.best_period)
        if best_stats:
            lines.append(f"  最大连续盈利:    {best_stats.max_consecutive_wins}")
            lines.append(f"  最大连续亏损:    {best_stats.max_consecutive_losses}")

        lines.append("")

        # 详细统计（最佳时间段）
        if best_stats:
            lines.append("-" * 70)
            lines.append(f"详细统计 ({report.best_period}秒持仓)")
            lines.append("-" * 70)
            lines.append(f"  盈利信号: {best_stats.profitable_signals} 个")
            lines.append(f"  亏损信号: {best_stats.losing_signals} 个")
            lines.append(f"  平均盈利: +{best_stats.avg_profit_usd:.2f} USDT "
                        f"({best_stats.avg_profit_percent:+.1f}%)")
            lines.append(f"  平均亏损: -{best_stats.avg_loss_usd:.2f} USDT "
                        f"({best_stats.avg_loss_percent:+.1f}%)")
            lines.append(f"  最大单笔盈利: +{best_stats.max_profit_usd:.2f} USDT")
            lines.append(f"  最大单笔亏损: -{best_stats.max_loss_usd:.2f} USDT")
            lines.append(f"  夏普比率: {best_stats.sharpe_ratio:.2f}")
            lines.append(f"  索提诺比率: {best_stats.sortino_ratio:.2f}")
            lines.append("")

        # 结论
        lines.append("-" * 70)
        lines.append("结论")
        lines.append("-" * 70)

        if report.tradeable_rate >= 60:
            lines.append(f"✓ 检测质量良好，{report.tradeable_rate:.0f}%的信号有盈利空间")
        elif report.tradeable_rate >= 40:
            lines.append(f"⚠ 检测质量一般，{report.tradeable_rate:.0f}%的信号有盈利空间，建议优化")
        else:
            lines.append(f"✗ 检测质量较差，仅{report.tradeable_rate:.0f}%的信号有盈利空间，需要重新设计")

        if report.net_profit_usd > 0:
            lines.append(f"✓ 总体盈利 ({report.best_period}秒持仓): {report.net_profit_usd:+.2f} USDT")
        else:
            lines.append(f"✗ 总体亏损 ({report.best_period}秒持仓): {report.net_profit_usd:+.2f} USDT")

        lines.append(f"建议优先使用 {report.best_period}秒 止盈策略")

        lines.append("")
        lines.append("=" * 70)
        lines.append("报告结束")
        lines.append("=" * 70)

        return "\n".join(lines)

    def print_report(self, report: AnalyticsReport) -> None:
        """打印报告到控制台"""
        text = self.generate_text_report(report)
        print(text)


# ============== 便捷函数 ==============

def analyze_records(records: List[PinSignalRecord]) -> AnalyticsReport:
    """分析信号记录"""
    analytics = SignalAnalytics()
    return analytics.analyze(records)


def print_analysis_report(records: List[PinSignalRecord]) -> None:
    """打印分析报告"""
    analytics = SignalAnalytics()
    report = analytics.analyze(records)
    generator = ReportGenerator()
    generator.print_report(report)
