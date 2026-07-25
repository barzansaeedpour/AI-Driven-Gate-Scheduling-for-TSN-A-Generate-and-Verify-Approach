import random
import z3
import numpy as np
from deap import base, creator, tools, algorithms

# ==========================================
# Phase 1: Setup
# ==========================================
STREAMS = [
    {"id": "Stream_A", "duration": 2, "deadline": 5,  "type": "Critical"},
    {"id": "Stream_B", "duration": 3, "deadline": 10, "type": "Critical"},
    {"id": "Stream_C", "duration": 4, "deadline": 15, "type": "Best-Effort"}
]
NUM_STREAMS = len(STREAMS)

# ==========================================
# Phase 2: Generate (Genetic Algorithm via DEAP)
# ==========================================
# Minimize violations
creator.create("FitnessMin", base.Fitness, weights=(-1.0,))
creator.create("Individual", list, fitness=creator.FitnessMin)

toolbox = base.Toolbox()
# Start times can be between 0 and 15
toolbox.register("attr_int", random.randint, 0, 15)
toolbox.register("individual", tools.initRepeat, creator.Individual, toolbox.attr_int, n=NUM_STREAMS)
toolbox.register("population", tools.initRepeat, list, toolbox.individual)

def evaluate(individual):
    penalty = 0
    # 1. Deadline penalty
    for i, s in enumerate(STREAMS):
        end_time = individual[i] + s["duration"]
        if end_time > s["deadline"]:
            penalty += (end_time - s["deadline"]) * 10 
            
    # 2. Collision penalty
    for i in range(NUM_STREAMS):
        for j in range(i + 1, NUM_STREAMS):
            start1, end1 = individual[i], individual[i] + STREAMS[i]["duration"]
            start2, end2 = individual[j], individual[j] + STREAMS[j]["duration"]
            # Overlap check
            if not (end1 <= start2 or end2 <= start1):
                overlap = min(end1, end2) - max(start1, start2)
                penalty += overlap * 20
                
    return (penalty,)

toolbox.register("evaluate", evaluate)
toolbox.register("mate", tools.cxTwoPoint)
toolbox.register("mutate", tools.mutUniformInt, low=0, up=15, indpb=0.2)
toolbox.register("select", tools.selTournament, tournsize=3)

def generate_schedule_ga():
    print("--- Phase 2: Generating Schedule (Genetic Algorithm) ---")
    pop = toolbox.population(n=20)
    hof = tools.HallOfFame(1)
    stats = tools.Statistics(lambda ind: ind.fitness.values)
    stats.register("min", np.min)
    
    # Run GA
    algorithms.eaSimple(pop, toolbox, cxpb=0.5, mutpb=0.2, ngen=10, stats=stats, halloffame=hof, verbose=False)
    
    best_ind = hof[0]
    schedule = {STREAMS[i]["id"]: best_ind[i] for i in range(NUM_STREAMS)}
    
    for s in STREAMS:
        print(f"GA Proposed -> {s['id']}: Start at {schedule[s['id']]}, Duration: {s['duration']}, Deadline: {s['deadline']}")
    
    return schedule

# ==========================================
# Phase 3: Verify (Formal Verification using Z3)
# ==========================================
def verify_schedule(streams, schedule):
    print("\n--- Phase 3: Formal Verification with Z3 ---")
    solver = z3.Solver()
    start_times = {s["id"]: z3.Int(f"start_{s['id']}") for s in streams}
    
    for i, s1 in enumerate(streams):
        st1 = start_times[s1["id"]]
        dur1 = s1["duration"]
        solver.add(st1 >= 0)
        solver.add(st1 + dur1 <= s1["deadline"]) # $ start\_time + duration \le deadline $
        
        for j, s2 in enumerate(streams):
            if i < j:
                st2 = start_times[s2["id"]]
                dur2 = s2["duration"]
                solver.add(z3.Or(st1 + dur1 <= st2, st2 + dur2 <= st1))
                
    assumptions = [start_times[s["id"]] == schedule[s["id"]] for s in streams]
    
    if solver.check(assumptions) == z3.sat:
        print("✅ VERIFIED: The GA schedule is mathematically VALID.")
        return True
    else:
        print("❌ FAILED: The GA schedule violates constraints.")
        return False

if __name__ == "__main__":
    max_loops = 3
    for loop in range(1, max_loops + 1):
        print(f"\n========== GA-VERIFY LOOP {loop} ==========")
        proposed = generate_schedule_ga()
        if verify_schedule(STREAMS, proposed):
            break
