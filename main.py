import random
import time
import math
import z3
import csv
import numpy as np
import networkx as nx
import gymnasium as gym
from gymnasium import spaces
from stable_baselines3 import PPO
from deap import base, creator, tools, algorithms
import matplotlib.pyplot as plt

# ==========================================
# Global Config
# ==========================================
GUARD_BAND = 1
MAX_INJ_TIME = 20
NUM_RUNS = 10
GLOBAL_SEEDS = [11, 22, 33, 44, 55, 66, 77, 88, 99, 111]

# ==========================================
# Phase 1: Setup Topology & Scenarios
# ==========================================
G = nx.DiGraph()
G.add_edge("ES1", "SW1", delay=1)
G.add_edge("ES2", "SW1", delay=1)
G.add_edge("SW1", "ES3", delay=1)
G.add_edge("SW1", "ES4", delay=1)
G.add_edge("SW1", "SW2", delay=2)
G.add_edge("SW2", "ES5", delay=1)

SCENARIOS = {
    "Small": [
        {"id": "Stream_A", "duration": 2, "deadline": 10, "path": [("ES1", "SW1"), ("SW1", "ES3")]},
        {"id": "Stream_B", "duration": 3, "deadline": 12, "path": [("ES2", "SW1"), ("SW1", "ES3")]},
        {"id": "Stream_C", "duration": 1, "deadline": 15, "path": [("ES1", "SW1"), ("SW1", "ES4")]}
    ],
    "Medium": [
        {"id": "Stream_A", "duration": 2, "deadline": 10, "path": [("ES1", "SW1"), ("SW1", "ES3")]},
        {"id": "Stream_B", "duration": 3, "deadline": 12, "path": [("ES2", "SW1"), ("SW1", "ES3")]},
        {"id": "Stream_C", "duration": 1, "deadline": 15, "path": [("ES1", "SW1"), ("SW1", "ES4")]},
        {"id": "Stream_D", "duration": 2, "deadline": 18, "path": [("ES2", "SW1"), ("SW1", "SW2"), ("SW2", "ES5")]},
        {"id": "Stream_E", "duration": 1, "deadline": 20, "path": [("ES1", "SW1"), ("SW1", "SW2"), ("SW2", "ES5")]}
    ]
}

# ==========================================
# Utility
# ==========================================
def set_all_seeds(seed):
    random.seed(seed)
    np.random.seed(seed)

def schedule_dict_to_list(schedule, streams):
    return [int(schedule[s["id"]]) for s in streams]

def schedule_list_to_dict(schedule_list, streams):
    return {streams[i]["id"]: int(schedule_list[i]) for i in range(len(streams))}

# ==========================================
# Core Evaluation Function
# ==========================================
def get_link_schedules(individual, streams):
    link_schedules = {}
    for i, s in enumerate(streams):
        current_time = int(individual[i])
        for link in s["path"]:
            if link not in link_schedules:
                link_schedules[link] = []
            start_on_link = current_time
            end_on_link = start_on_link + s["duration"]
            link_schedules[link].append((start_on_link, end_on_link, s["id"]))
            current_time = end_on_link + G.edges[link]["delay"]
    return link_schedules

def get_stream_finish_times(individual, streams):
    finish_times = {}
    for i, s in enumerate(streams):
        current_time = int(individual[i])
        for link in s["path"]:
            start_on_link = current_time
            end_on_link = start_on_link + s["duration"]
            current_time = end_on_link + G.edges[link]["delay"]
        finish_times[s["id"]] = end_on_link
    return finish_times

def compute_metrics(individual, streams):
    penalty = 0
    collision_count = 0
    deadline_violations = 0
    total_deadline_lateness = 0

    link_schedules = get_link_schedules(individual, streams)
    finish_times = get_stream_finish_times(individual, streams)

    latencies = []
    for i, s in enumerate(streams):
        finish_time = finish_times[s["id"]]
        latency = finish_time - int(individual[i])
        latencies.append(latency)
        if finish_time > s["deadline"]:
            lateness = finish_time - s["deadline"]
            penalty += lateness * 10
            deadline_violations += 1
            total_deadline_lateness += lateness

    for link, schedules in link_schedules.items():
        n = len(schedules)
        for i in range(n):
            for j in range(i + 1, n):
                start1, end1, sid1 = schedules[i]
                start2, end2, sid2 = schedules[j]
                if not (end1 + GUARD_BAND <= start2 or end2 + GUARD_BAND <= start1):
                    overlap = min(end1 + GUARD_BAND, end2 + GUARD_BAND) - max(start1, start2)
                    if overlap > 0:
                        penalty += overlap * 20
                        collision_count += 1

    total_latency = sum(latencies)
    max_latency = max(latencies) if latencies else 0
    feasible = (penalty == 0)

    return {
        "penalty": penalty,
        "feasible": feasible,
        "collision_count": collision_count,
        "deadline_violations": deadline_violations,
        "total_deadline_lateness": total_deadline_lateness,
        "total_latency": total_latency,
        "max_latency": max_latency
    }

def calculate_penalty(individual, streams):
    return compute_metrics(individual, streams)["penalty"]

# ==========================================
# Conflict Analysis + Repair
# ==========================================
def analyze_conflicts(individual, streams):
    conflicts = {
        "deadline_streams": [],
        "collision_pairs": []
    }

    link_schedules = get_link_schedules(individual, streams)
    finish_times = get_stream_finish_times(individual, streams)

    for i, s in enumerate(streams):
        finish_time = finish_times[s["id"]]
        if finish_time > s["deadline"]:
            conflicts["deadline_streams"].append({
                "stream_id": s["id"],
                "finish_time": finish_time,
                "deadline": s["deadline"],
                "lateness": finish_time - s["deadline"]
            })

    for link, schedules in link_schedules.items():
        n = len(schedules)
        for i in range(n):
            for j in range(i + 1, n):
                start1, end1, sid1 = schedules[i]
                start2, end2, sid2 = schedules[j]
                if not (end1 + GUARD_BAND <= start2 or end2 + GUARD_BAND <= start1):
                    overlap = min(end1 + GUARD_BAND, end2 + GUARD_BAND) - max(start1, start2)
                    if overlap > 0:
                        conflicts["collision_pairs"].append({
                            "link": link,
                            "stream1": sid1,
                            "stream2": sid2,
                            "overlap": overlap
                        })

    return conflicts

def repair_schedule(schedule_list, streams, max_repair_iter=50):
    repaired = list(schedule_list)
    best_metrics = compute_metrics(repaired, streams)
    best_penalty = best_metrics["penalty"]

    stream_idx = {s["id"]: i for i, s in enumerate(streams)}

    for _ in range(max_repair_iter):
        conflicts = analyze_conflicts(repaired, streams)
        if not conflicts["deadline_streams"] and not conflicts["collision_pairs"]:
            break

        changed = False

        # Repair deadlines first
        for dconf in conflicts["deadline_streams"]:
            sid = dconf["stream_id"]
            idx = stream_idx[sid]
            shift = dconf["lateness"]
            new_time = max(0, repaired[idx] - shift)
            if new_time != repaired[idx]:
                repaired[idx] = new_time
                changed = True

        # Repair collisions
        for cconf in conflicts["collision_pairs"]:
            sid1 = cconf["stream1"]
            sid2 = cconf["stream2"]
            idx1 = stream_idx[sid1]
            idx2 = stream_idx[sid2]

            cand1 = list(repaired)
            cand2 = list(repaired)

            cand1[idx1] = max(0, min(MAX_INJ_TIME, cand1[idx1] + cconf["overlap"] + GUARD_BAND))
            cand2[idx2] = max(0, min(MAX_INJ_TIME, cand2[idx2] + cconf["overlap"] + GUARD_BAND))

            p1 = calculate_penalty(cand1, streams)
            p2 = calculate_penalty(cand2, streams)

            if p1 < p2 and p1 <= calculate_penalty(repaired, streams):
                repaired = cand1
                changed = True
            elif p2 <= p1 and p2 <= calculate_penalty(repaired, streams):
                repaired = cand2
                changed = True

        current_metrics = compute_metrics(repaired, streams)
        current_penalty = current_metrics["penalty"]

        if current_penalty < best_penalty:
            best_penalty = current_penalty
            best_metrics = current_metrics

        if not changed:
            break

    return repaired, best_metrics

# ==========================================
# Phase 2A: Genetic Algorithm (DEAP)
# ==========================================
def run_ga(streams):
    num_streams = len(streams)

    if hasattr(creator, "FitnessMin"):
        try:
            del creator.FitnessMin
            del creator.Individual
        except:
            pass

    creator.create("FitnessMin", base.Fitness, weights=(-1.0,))
    creator.create("Individual", list, fitness=creator.FitnessMin)

    toolbox = base.Toolbox()
    toolbox.register("attr_int", random.randint, 0, MAX_INJ_TIME)
    toolbox.register("individual", tools.initRepeat, creator.Individual, toolbox.attr_int, n=num_streams)
    toolbox.register("population", tools.initRepeat, list, toolbox.individual)
    toolbox.register("evaluate", lambda ind: (calculate_penalty(ind, streams),))
    toolbox.register("mate", tools.cxTwoPoint)
    toolbox.register("mutate", tools.mutUniformInt, low=0, up=MAX_INJ_TIME, indpb=0.2)
    toolbox.register("select", tools.selTournament, tournsize=3)

    pop = toolbox.population(n=40)
    hof = tools.HallOfFame(1)

    start_time = time.time()
    algorithms.eaSimple(pop, toolbox, cxpb=0.5, mutpb=0.2, ngen=30, halloffame=hof, verbose=False)
    exec_time = time.time() - start_time

    best_ind = list(hof[0])
    schedule = schedule_list_to_dict(best_ind, streams)
    return schedule, compute_metrics(best_ind, streams), exec_time

def run_ga_with_repair(streams):
    schedule, metrics, exec_time = run_ga(streams)
    repaired_list, repaired_metrics = repair_schedule(schedule_dict_to_list(schedule, streams), streams)
    repaired_schedule = schedule_list_to_dict(repaired_list, streams)
    return repaired_schedule, repaired_metrics, exec_time

# ==========================================
# Phase 2B: Reinforcement Learning (PPO)
# ==========================================
class TSNScheduleEnv(gym.Env):
    def __init__(self, streams):
        super(TSNScheduleEnv, self).__init__()
        self.streams = streams
        self.num_streams = len(streams)
        self.action_space = spaces.Discrete(MAX_INJ_TIME + 1)
        self.observation_space = spaces.Box(low=-1, high=MAX_INJ_TIME, shape=(self.num_streams,), dtype=np.int32)

    def reset(self, seed=None, options=None):
        super().reset(seed=seed)
        self.state = np.full(self.num_streams, -1, dtype=np.int32)
        self.current_stream = 0
        return self.state, {}

    def partial_penalty(self):
        partial_state = self.state.copy()
        assigned_indices = [i for i, v in enumerate(partial_state) if v >= 0]
        if len(assigned_indices) == 0:
            return 0

        temp_streams = [self.streams[i] for i in assigned_indices]
        temp_schedule = [partial_state[i] for i in assigned_indices]
        return calculate_penalty(temp_schedule, temp_streams)

    def step(self, action):
        self.state[self.current_stream] = int(action)

        prev_partial_penalty = self.partial_penalty()
        self.current_stream += 1
        new_partial_penalty = self.partial_penalty()

        done = bool(self.current_stream == self.num_streams)

        # Dense reward shaping
        reward = -(new_partial_penalty - prev_partial_penalty)

        if done:
            metrics = compute_metrics(self.state, self.streams)
            reward += -metrics["penalty"]
            if metrics["feasible"]:
                reward += 100

        return self.state, reward, done, False, {}

def run_rl(streams):
    env = TSNScheduleEnv(streams)
    model = PPO("MlpPolicy", env, verbose=0, n_steps=128, batch_size=64)

    train_start = time.time()
    model.learn(total_timesteps=7000)
    train_time = time.time() - train_start

    inf_start = time.time()
    obs, _ = env.reset()
    done = False
    while not done:
        action, _states = model.predict(obs, deterministic=True)
        obs, reward, done, truncated, info = env.step(action)
    inf_time = time.time() - inf_start

    schedule = schedule_list_to_dict(obs, streams)
    return schedule, compute_metrics(obs, streams), train_time, inf_time

def run_rl_with_repair(streams):
    schedule, metrics, train_time, inf_time = run_rl(streams)
    repaired_list, repaired_metrics = repair_schedule(schedule_dict_to_list(schedule, streams), streams)
    repaired_schedule = schedule_list_to_dict(repaired_list, streams)
    return repaired_schedule, repaired_metrics, train_time, inf_time

# ==========================================
# Phase 2C: Simulated Annealing (SA)
# ==========================================
def run_sa(streams, initial_temp=200.0, cooling_rate=0.995, max_iter=5000):
    num_streams = len(streams)

    start_time = time.time()
    current_solution = [random.randint(0, MAX_INJ_TIME) for _ in range(num_streams)]
    current_penalty = calculate_penalty(current_solution, streams)

    best_solution = list(current_solution)
    best_penalty = current_penalty
    temp = initial_temp

    for _ in range(max_iter):
        if temp < 0.1 or best_penalty == 0:
            break

        neighbor = list(current_solution)
        idx = random.randint(0, num_streams - 1)

        if random.random() < 0.3:
            neighbor[idx] = random.randint(0, MAX_INJ_TIME)
        else:
            change = random.choice([-3, -2, -1, 1, 2, 3])
            neighbor[idx] = max(0, min(MAX_INJ_TIME, neighbor[idx] + change))

        neighbor_penalty = calculate_penalty(neighbor, streams)

        if neighbor_penalty < current_penalty:
            current_solution = neighbor
            current_penalty = neighbor_penalty
            if current_penalty < best_penalty:
                best_solution = list(current_solution)
                best_penalty = current_penalty
        else:
            delta = neighbor_penalty - current_penalty
            prob = math.exp(-delta / temp) if temp > 1e-9 else 0
            if random.random() < prob:
                current_solution = neighbor
                current_penalty = neighbor_penalty

        temp *= cooling_rate

    exec_time = time.time() - start_time
    schedule = schedule_list_to_dict(best_solution, streams)
    return schedule, compute_metrics(best_solution, streams), exec_time

def run_sa_with_repair(streams):
    schedule, metrics, exec_time = run_sa(streams)
    repaired_list, repaired_metrics = repair_schedule(schedule_dict_to_list(schedule, streams), streams)
    repaired_schedule = schedule_list_to_dict(repaired_list, streams)
    return repaired_schedule, repaired_metrics, exec_time

# ==========================================
# Phase 2D: Z3-Only Scheduler
# ==========================================
def run_z3_solver(streams, timeout_ms=5000):
    solver = z3.Optimize()
    solver.set(timeout=timeout_ms)

    inj_times = {s["id"]: z3.Int(f"inj_{s['id']}") for s in streams}

    for s in streams:
        solver.add(inj_times[s["id"]] >= 0)
        solver.add(inj_times[s["id"]] <= MAX_INJ_TIME)

    # Deadline constraints
    for s in streams:
        current_sym_time = inj_times[s["id"]]
        for link in s["path"]:
            current_sym_time = current_sym_time + s["duration"] + G.edges[link]["delay"]
        finish_expr = current_sym_time - G.edges[s["path"][-1]]["delay"]
        solver.add(finish_expr <= s["deadline"])

    # Non-overlap constraints
    for i, s1 in enumerate(streams):
        for j, s2 in enumerate(streams):
            if i < j:
                shared_links = set(s1["path"]).intersection(set(s2["path"]))
                for link in shared_links:
                    t1 = inj_times[s1["id"]]
                    for l1 in s1["path"]:
                        if l1 == link:
                            break
                        t1 = t1 + s1["duration"] + G.edges[l1]["delay"]

                    t2 = inj_times[s2["id"]]
                    for l2 in s2["path"]:
                        if l2 == link:
                            break
                        t2 = t2 + s2["duration"] + G.edges[l2]["delay"]

                    solver.add(z3.Or(
                        t1 + s1["duration"] + GUARD_BAND <= t2,
                        t2 + s2["duration"] + GUARD_BAND <= t1
                    ))

    total_inj = z3.Sum([inj_times[s["id"]] for s in streams])
    solver.minimize(total_inj)

    start_time = time.time()
    status = solver.check()
    exec_time = time.time() - start_time

    if status == z3.sat:
        model = solver.model()
        schedule = {s["id"]: model[inj_times[s["id"]]].as_long() for s in streams}
        metrics = compute_metrics(schedule_dict_to_list(schedule, streams), streams)
        return schedule, metrics, exec_time, True
    else:
        fallback = {s["id"]: 0 for s in streams}
        metrics = compute_metrics(schedule_dict_to_list(fallback, streams), streams)
        return fallback, metrics, exec_time, False

# ==========================================
# Phase 3: Formal Verification (Z3)
# ==========================================
def verify_schedule(schedule, streams):
    solver = z3.Solver()
    inj_times = {s["id"]: z3.Int(f"inj_{s['id']}") for s in streams}

    for s1 in streams:
        solver.add(inj_times[s1["id"]] >= 0)
        current_sym_time = inj_times[s1["id"]]
        for link in s1["path"]:
            current_sym_time = current_sym_time + s1["duration"] + G.edges[link]["delay"]
        solver.add(current_sym_time - G.edges[s1["path"][-1]]["delay"] <= s1["deadline"])

    for i, s1 in enumerate(streams):
        for j, s2 in enumerate(streams):
            if i < j:
                shared_links = set(s1["path"]).intersection(set(s2["path"]))
                for link in shared_links:
                    t1 = inj_times[s1["id"]]
                    for l1 in s1["path"]:
                        if l1 == link:
                            break
                        t1 = t1 + s1["duration"] + G.edges[l1]["delay"]

                    t2 = inj_times[s2["id"]]
                    for l2 in s2["path"]:
                        if l2 == link:
                            break
                        t2 = t2 + s2["duration"] + G.edges[l2]["delay"]

                    solver.add(z3.Or(
                        t1 + s1["duration"] + GUARD_BAND <= t2,
                        t2 + s2["duration"] + GUARD_BAND <= t1
                    ))

    assumptions = [inj_times[s["id"]] == schedule[s["id"]] for s in streams]
    return solver.check(assumptions) == z3.sat

# ==========================================
# Experiment Runner
# ==========================================
def run_single_algorithm(algo_name, streams):
    if algo_name == "GA":
        schedule, metrics, t = run_ga(streams)
        return schedule, metrics, "-", t
    elif algo_name == "GA_Repair":
        schedule, metrics, t = run_ga_with_repair(streams)
        return schedule, metrics, "-", t
    elif algo_name == "RL_PPO":
        schedule, metrics, train_t, inf_t = run_rl(streams)
        return schedule, metrics, train_t, inf_t
    elif algo_name == "RL_PPO_Repair":
        schedule, metrics, train_t, inf_t = run_rl_with_repair(streams)
        return schedule, metrics, train_t, inf_t
    elif algo_name == "SA":
        schedule, metrics, t = run_sa(streams)
        return schedule, metrics, "-", t
    elif algo_name == "SA_Repair":
        schedule, metrics, t = run_sa_with_repair(streams)
        return schedule, metrics, "-", t
    elif algo_name == "Z3_Only":
        schedule, metrics, t, sat_found = run_z3_solver(streams)
        return schedule, metrics, "-", t
    else:
        raise ValueError(f"Unknown algorithm: {algo_name}")

def summarize_results(raw_results):
    grouped = {}
    for row in raw_results:
        key = (row["Scenario"], row["Algorithm"])
        if key not in grouped:
            grouped[key] = []
        grouped[key].append(row)

    summary = []
    for (scenario, algo), rows in grouped.items():
        penalties = [r["Penalty"] for r in rows]
        verifieds = [1 if r["Z3_Verified"] else 0 for r in rows]
        total_latencies = [r["Total_Latency"] for r in rows]
        max_latencies = [r["Max_Latency"] for r in rows]
        runtimes = [r["Inference_or_Search_Time"] for r in rows if isinstance(r["Inference_or_Search_Time"], (int, float))]
        train_times = [r["Train_Time"] for r in rows if isinstance(r["Train_Time"], (int, float))]

        summary.append({
            "Scenario": scenario,
            "Algorithm": algo,
            "Penalty_Mean": round(float(np.mean(penalties)), 4),
            "Penalty_Std": round(float(np.std(penalties)), 4),
            "Success_Rate": round(float(np.mean(verifieds)), 4),
            "Latency_Mean": round(float(np.mean(total_latencies)), 4),
            "MaxLatency_Mean": round(float(np.mean(max_latencies)), 4),
            "Runtime_Mean": round(float(np.mean(runtimes)), 6) if runtimes else "-",
            "Runtime_Std": round(float(np.std(runtimes)), 6) if runtimes else "-",
            "TrainTime_Mean": round(float(np.mean(train_times)), 6) if train_times else "-"
        })
    return summary

# ==========================================
# Plotting Functions
# ==========================================
def generate_academic_plots(summary_results):
    scenarios = list(SCENARIOS.keys())
    algos = ["GA", "GA_Repair", "RL_PPO", "RL_PPO_Repair", "SA", "SA_Repair", "Z3_Only"]

    penalty_map = {algo: [] for algo in algos}
    success_map = {algo: [] for algo in algos}
    runtime_map = {algo: [] for algo in algos}

    for sc in scenarios:
        for al in algos:
            row = next((r for r in summary_results if r["Scenario"] == sc and r["Algorithm"] == al), None)
            if row is not None:
                penalty_map[al].append(row["Penalty_Mean"])
                success_map[al].append(row["Success_Rate"])
                runtime_map[al].append(row["Runtime_Mean"] if row["Runtime_Mean"] != "-" else 0.0001)
            else:
                penalty_map[al].append(0)
                success_map[al].append(0)
                runtime_map[al].append(0.0001)

    x = np.arange(len(scenarios))
    width = 0.11

    plt.rcParams.update({'font.size': 10, 'font.family': 'serif'})

    # Penalty Plot
    fig, ax = plt.subplots(figsize=(10, 5))
    for idx, algo in enumerate(algos):
        ax.bar(x + (idx - 3) * width, penalty_map[algo], width, label=algo, edgecolor='black')
    ax.set_ylabel('Mean Penalty')
    ax.set_title('Mean Penalty Comparison Across Scenarios')
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios)
    ax.legend(fontsize=8)
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.savefig("Results/mean_penalty_comparison.pdf")
    plt.savefig("Results/mean_penalty_comparison.png", dpi=300)
    plt.close()

    # Success Rate Plot
    fig, ax = plt.subplots(figsize=(10, 5))
    for idx, algo in enumerate(algos):
        ax.bar(x + (idx - 3) * width, success_map[algo], width, label=algo, edgecolor='black')
    ax.set_ylabel('Success Rate (Z3 Verified)')
    ax.set_title('Success Rate Comparison Across Scenarios')
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios)
    ax.set_ylim([0, 1.05])
    ax.legend(fontsize=8)
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.savefig("Results/success_rate_comparison.pdf")
    plt.savefig("Results/success_rate_comparison.png", dpi=300)
    plt.close()

    # Runtime Plot
    fig, ax = plt.subplots(figsize=(10, 5))
    for idx, algo in enumerate(algos):
        ax.bar(x + (idx - 3) * width, runtime_map[algo], width, label=algo, edgecolor='black')
    ax.set_ylabel('Mean Runtime (s)')
    ax.set_title('Runtime Comparison Across Scenarios')
    ax.set_xticks(x)
    ax.set_xticklabels(scenarios)
    ax.set_yscale('log')
    ax.legend(fontsize=8)
    ax.grid(axis='y', linestyle='--', alpha=0.7)
    plt.tight_layout()
    plt.savefig("Results/runtime_comparison.pdf")
    plt.savefig("Results/runtime_comparison.png", dpi=300)
    plt.close()

    print("\n✅ Academic plots generated successfully.")

# ==========================================
# Save Schedules
# ==========================================
def save_schedule_details(filename, schedule_rows):
    with open(filename, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow(["Scenario", "Algorithm", "Run", "Schedule"])
        for row in schedule_rows:
            writer.writerow(row)

# ==========================================
# Main
# ==========================================
if __name__ == "__main__":
    algorithms_to_run = ["GA", "GA_Repair", "RL_PPO", "RL_PPO_Repair", "SA", "SA_Repair", "Z3_Only"]

    raw_results = []
    schedule_rows = []

    for scenario_name, current_streams in SCENARIOS.items():
        print(f"\n{'='*80}")
        print(f"Running Scenario: {scenario_name} ({len(current_streams)} Streams)")
        print(f"{'='*80}")

        for run_id in range(NUM_RUNS):
            seed = GLOBAL_SEEDS[run_id]
            set_all_seeds(seed)

            print(f"\n[Scenario={scenario_name}] Run {run_id+1}/{NUM_RUNS}, Seed={seed}")

            for algo_name in algorithms_to_run:
                print(f"   -> Running {algo_name} ...", end=" ")
                schedule, metrics, train_time, inf_time = run_single_algorithm(algo_name, current_streams)
                verified = verify_schedule(schedule, current_streams)

                raw_results.append({
                    "Scenario": scenario_name,
                    "Algorithm": algo_name,
                    "Run": run_id + 1,
                    "Seed": seed,
                    "Penalty": metrics["penalty"],
                    "Feasible": metrics["feasible"],
                    "Z3_Verified": verified,
                    "Collision_Count": metrics["collision_count"],
                    "Deadline_Violations": metrics["deadline_violations"],
                    "Total_Deadline_Lateness": metrics["total_deadline_lateness"],
                    "Total_Latency": metrics["total_latency"],
                    "Max_Latency": metrics["max_latency"],
                    "Train_Time": train_time if isinstance(train_time, (int, float)) else "-",
                    "Inference_or_Search_Time": inf_time
                })

                schedule_rows.append([
                    scenario_name,
                    algo_name,
                    run_id + 1,
                    str(schedule)
                ])

                print(f"Penalty={metrics['penalty']}, Verified={verified}, Time={inf_time}")

    # Save raw results
    raw_csv = "Results/raw_comparison_results.csv"
    with open(raw_csv, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            "Scenario", "Algorithm", "Run", "Seed", "Penalty", "Feasible", "Z3_Verified",
            "Collision_Count", "Deadline_Violations", "Total_Deadline_Lateness",
            "Total_Latency", "Max_Latency", "Train_Time", "Inference_or_Search_Time"
        ])
        for r in raw_results:
            writer.writerow([
                r["Scenario"], r["Algorithm"], r["Run"], r["Seed"], r["Penalty"], r["Feasible"], r["Z3_Verified"],
                r["Collision_Count"], r["Deadline_Violations"], r["Total_Deadline_Lateness"],
                r["Total_Latency"], r["Max_Latency"], r["Train_Time"], r["Inference_or_Search_Time"]
            ])

    print(f"\n✅ Raw results saved to {raw_csv}")

    # Save schedules
    schedule_csv = "Results/schedule_details.csv"
    save_schedule_details(schedule_csv, schedule_rows)
    print(f"✅ Schedule details saved to {schedule_csv}")

    # Summary
    summary_results = summarize_results(raw_results)

    summary_csv = "Results/summary_results.csv"
    with open(summary_csv, mode='w', newline='') as file:
        writer = csv.writer(file)
        writer.writerow([
            "Scenario", "Algorithm", "Penalty_Mean", "Penalty_Std", "Success_Rate",
            "Latency_Mean", "MaxLatency_Mean", "Runtime_Mean", "Runtime_Std", "TrainTime_Mean"
        ])
        for r in summary_results:
            writer.writerow([
                r["Scenario"], r["Algorithm"], r["Penalty_Mean"], r["Penalty_Std"], r["Success_Rate"],
                r["Latency_Mean"], r["MaxLatency_Mean"], r["Runtime_Mean"], r["Runtime_Std"], r["TrainTime_Mean"]
            ])

    print(f"✅ Summary results saved to {summary_csv}")

    # Print summary to terminal
    print("\n" + "="*130)
    print("FINAL SUMMARY RESULTS")
    print("="*130)
    print(f"{'Scenario':<10} | {'Algorithm':<15} | {'Penalty Mean':<12} | {'Penalty Std':<12} | {'Success Rate':<12} | {'Latency Mean':<12} | {'Runtime Mean':<12}")
    print("-"*130)
    for r in summary_results:
        print(f"{r['Scenario']:<10} | {r['Algorithm']:<15} | {r['Penalty_Mean']:<12} | {r['Penalty_Std']:<12} | {r['Success_Rate']:<12} | {r['Latency_Mean']:<12} | {str(r['Runtime_Mean']):<12}")
    print("="*130)

    # Generate plots
    generate_academic_plots(summary_results)
