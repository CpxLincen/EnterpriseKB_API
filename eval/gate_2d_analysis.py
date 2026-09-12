"""防幻觉门禁：二维联合阈值分析。

读取 eval/gate-signals.tsv（09-05 会话采集的 50 题检索信号），
分析稠密距离 + Rerank 分数（以及 BM25 分）能否用联合阈值把
“负面（应拒答）”与“事实（应回答）”完全分开。

运行：.venv\\Scripts\\python eval/gate_2d_analysis.py
"""

from __future__ import annotations

import csv
import itertools
import math
import sys
from dataclasses import dataclass

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.svm import LinearSVC, SVC
from sklearn.preprocessing import StandardScaler


@dataclass
class Sample:
    sid: str
    category: str  # factual / negative
    difficulty: str
    dense_dist: float
    dense_sim: float
    bm25: float
    rerank: float
    question: str

    @property
    def label(self) -> int:
        # 1 = 事实（应回答/放行），0 = 负面（应拒答/拦截）
        return 1 if self.category == "factual" else 0


def load(path: str) -> list[Sample]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f, delimiter="\t"):
            rows.append(
                Sample(
                    sid=r["id"],
                    category=r["category"],
                    difficulty=r["difficulty"],
                    dense_dist=float(r["dense_dist"]),
                    dense_sim=float(r["dense_sim"]),
                    bm25=float(r["bm25_top"]),
                    rerank=float(r["rerank_top"]),
                    question=r["question"],
                )
            )
    return rows


def confusion(y_true: np.ndarray, y_pred: np.ndarray) -> tuple[int, int, int, int]:
    """返回 (TP, FP, TN, FN)，其中 positive=负面(应拦截)，negative=事实(应放行)。"""
    neg_mask = y_true == 0
    pos_mask = y_true == 1
    tp = int(np.sum((y_pred == 0) & neg_mask))  # 拦截对了负面
    fp = int(np.sum((y_pred == 0) & pos_mask))  # 误拦了事实
    tn = int(np.sum((y_pred == 1) & pos_mask))  # 放行对了事实
    fn = int(np.sum((y_pred == 1) & neg_mask))  # 漏放了负面
    return tp, fp, tn, fn


def report(name: str, y_true: np.ndarray, y_pred: np.ndarray, samples: list[Sample]):
    tp, fp, tn, fn = confusion(y_true, y_pred)
    miss_neg = [s.sid for s, p in zip(samples, y_pred) if s.category == "negative" and p == 1]
    miss_fact = [s.sid for s, p in zip(samples, y_pred) if s.category == "factual" and p == 0]
    rec = tp / (tp + fn) if tp + fn else 0.0
    prec = tp / (tp + fp) if tp + fp else 0.0
    print(
        f"[{name}] 拦截负面 {tp}/{tp + fn}  误拦事实 {fp}/{fp + tn}  "
        f"(召回={rec:.3f}, 精确={prec:.3f})"
    )
    if miss_neg:
        print(f"    漏放的负面: {', '.join(miss_neg)}")
    if miss_fact:
        print(f"    误拦的事实: {', '.join(miss_fact)}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    path = sys.argv[1] if len(sys.argv) > 1 else "eval/gate-signals.tsv"
    print(f"analyzing {path}")
    samples = load(path)
    negatives = [s for s in samples if s.category == "negative"]
    factuals = [s for s in samples if s.category == "factual"]
    y = np.array([s.label for s in samples])
    print(f"共 {len(samples)} 题：负面 {len(negatives)}，事实 {len(factuals)}")
    print()

    # ---------- 0. 单维阈值：Rerank 分数（当前 rerank_floor=0.05） ----------
    for sid in sorted({s.sid for s in negatives}):
        pass
    print("== 0. 负面与最弱事实题的两维坐标（dense_sim, rerank, dense_dist, bm25） ==")
    weak_fact = sorted(factuals, key=lambda s: s.rerank)[:8]
    for s in sorted(negatives, key=lambda s: -s.rerank):
        print(f"  {s.sid:8s} 负面  sim={s.dense_sim:.4f} dist={s.dense_dist:.4f} rerank={s.rerank:.4f} bm25={s.bm25:7.2f}")
    print("  " + "-" * 72)
    for s in weak_fact:
        print(f"  {s.sid:8s} 事实  sim={s.dense_sim:.4f} dist={s.dense_dist:.4f} rerank={s.rerank:.4f} bm25={s.bm25:7.2f}")
    print()

    # ---------- 1. 当前单阈值：rerank_floor=0.05 ----------
    print("== 1. 单维 Rerank 阈值（当前 floor=0.05） ==")
    pred = np.where(np.array([s.rerank for s in samples]) < 0.05, 0, 1)
    report("rerank<0.05 拦截", y, pred, samples)
    print()

    # ---------- 2. 单维最优阈值扫描（每个特征） ----------
    print("== 2. 单维最优阈值扫描（使漏放负面 + 误拦事实 最小） ==")
    feats = {
        "dense_sim": [s.dense_sim for s in samples],
        "dense_dist": [s.dense_dist for s in samples],
        "bm25": [s.bm25 for s in samples],
        "rerank": [s.rerank for s in samples],
    }
    for fname, vals in feats.items():
        best = None
        uniq = sorted(set(vals))
        for t in uniq:
            # “值越小越可疑”方向：< t 则拦截（对 dense_sim/bm25/rerank 成立；dense_dist 用 > t 拦截）
            if fname == "dense_dist":
                p = np.where(np.array(vals) > t, 0, 1)
            else:
                p = np.where(np.array(vals) < t, 0, 1)
            tp, fp, tn, fn = confusion(y, p)
            score = (tp + fp, fn, fp)  # 先看总错数（fp+fn），再看漏放负面
            if best is None or (fp + fn, fn, fp) < best[1]:
                best = ((t, tp, fp, tn, fn), (fp + fn, fn, fp))
        t, tp, fp, tn, fn = best[0]
        print(
            f"  {fname:10s} 最优阈值 {t:.4g} -> 拦截负面 {tp}/7, 误拦事实 {fp}/43 "
            f"(总错 {fp+fn}, 漏放负面 {fn})"
        )
    print()

    # ---------- 3. 二维线性可分性检验 ----------
    print("== 3. 二维/三维 线性分类器可分性（训练集完全拟合=线性可分） ==")
    pairs = [
        ("dense_sim + rerank", np.array([[s.dense_sim, s.rerank] for s in samples])),
        ("dense_dist + rerank", np.array([[s.dense_dist, s.rerank] for s in samples])),
        ("dense_sim + bm25 + rerank", np.array([[s.dense_sim, s.bm25, s.rerank] for s in samples])),
        ("dense_dist + bm25 + rerank", np.array([[s.dense_dist, s.bm25, s.rerank] for s in samples])),
    ]
    for name, X in pairs:
        Xs = StandardScaler().fit_transform(X)
        accs = {}
        for clf_name, clf in [
            ("LogReg", LogisticRegression(max_iter=2000)),
            ("LinearSVC", LinearSVC(max_iter=20000)),
            ("SVC-rbf", SVC(kernel="rbf")),
        ]:
            clf.fit(Xs, y)
            accs[clf_name] = float(clf.score(Xs, y))
        print(f"  {name:24s} 训练集准确率 -> " + " | ".join(f"{k}={v:.3f}" for k, v in accs.items()))
    print()

    # ---------- 4. 二维网格联合阈值（矩形/角落规则） ----------
    # 规则：拦截 当 (rerank < tr) 或 (dense_sim < ts 且 rerank < tr2) 之类；
    # 这里直接对 (dense_sim, rerank) 做“双阈值矩形”搜索：
    #   拦截条件 = (dense_sim < ts) AND (rerank < tr)   （两维都弱才拦）
    print("== 4. 二维矩形规则：拦截条件 = (dense_sim < Ts) AND (rerank < Tr) ==")
    best = None
    sims = sorted(set(s.dense_sim for s in samples))
    rrs = sorted(set(s.rerank for s in samples))
    for ts in sims:
        for tr in rrs:
            p = np.array([0 if (s.dense_sim < ts and s.rerank < tr) else 1 for s in samples])
            tp, fp, tn, fn = confusion(y, p)
            if best is None or (fp + fn, fn, fp) < best[1]:
                best = ((ts, tr, tp, fp, tn, fn), (fp + fn, fn, fp))
    ts, tr, tp, fp, tn, fn = best[0]
    p = np.array([0 if (s.dense_sim < ts and s.rerank < tr) else 1 for s in samples])
    report(f"AND 规则 Ts={ts:.4f} Tr={tr:.4f}", y, p, samples)
    print()

    # ---------- 5. 线性组合网格：score = w*rerank + (1-w)*dense_sim，找阈值 ----------
    print("== 5. 加权和 score = w*rerank + (1-w)*dense_sim，score < T 拦截 ==")
    best = None
    for w in np.linspace(0.0, 1.0, 101):
        sc = w * np.array([s.rerank for s in samples]) + (1 - w) * np.array([s.dense_sim for s in samples])
        for t in sorted(set(sc)):
            p = np.where(sc < t, 0, 1)
            tp, fp, tn, fn = confusion(y, p)
            if best is None or (fp + fn, fn, fp) < best[1]:
                best = ((w, t, tp, fp, tn, fn), (fp + fn, fn, fp))
    w, t, tp, fp, tn, fn = best[0]
    sc = w * np.array([s.rerank for s in samples]) + (1 - w) * np.array([s.dense_sim for s in samples])
    p = np.where(sc < t, 0, 1)
    report(f"加权和 w={w:.3f} T={t:.4f}", y, p, samples)
    print()

    # ---------- 6. 结论性统计：负面与最弱事实的重叠度 ----------
    print("== 6. 重叠诊断 ==")
    top_neg = max(s.rerank for s in negatives)
    weak_f = sorted(factuals, key=lambda s: s.rerank)[:3]
    print(f"  负面最高 rerank = {top_neg:.4f} ({[s.sid for s in negatives if s.rerank == top_neg]})")
    for s in weak_f:
        print(f"  最弱事实 {s.sid}: rerank={s.rerank:.4f} dense_sim={s.dense_sim:.4f}")
    # 每个负面在 (dense_sim, rerank) 中是否被某个事实“双维同时更弱”地包围
    print()
    for n in negatives:
        dom_facts = [f.sid for f in factuals if f.dense_sim <= n.dense_sim and f.rerank <= n.rerank]
        print(f"  {n.sid}: 存在双维均弱于它的弱事实 -> {dom_facts if dom_facts else '无'}")


if __name__ == "__main__":
    main()
