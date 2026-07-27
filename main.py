import random
import z3
import numpy as np
import networkx as nx
import xml.etree.ElementTree as ET
from xml.dom import minidom
from deap import base, creator, tools, algorithms

# ==========================================
# Phase 1: Setup Topology & Streams
# ==========================================
# 1. Define Network Topology
G = nx.DiGraph()
# Adding edges with transmission+propagation delay
G.add_edge("ES1", "SW1", delay=1)
G.add_edge("ES2", "SW1", delay=1)
G.add_edge("SW1", "ES3", delay=1)
G.add_edge("SW1", "ES4", delay=1)

# 2. Define Streams with Routing Paths
STREAMS = [
    {"id": "Stream_A", "duration": 2, "deadline": 10, "path": [("ES1", "SW1"), ("SW1", "ES3")]},
    {"id": "Stream_B", "duration": 3, "deadline": 12, "path": [("ES2", "SW1"), ("SW1", "ES3")]}, # Shares (SW1->ES3) with A
    {"id": "Stream_C", "duration": 1, "deadline": 15, "path": [("ES1", "SW1"), ("SW1", "ES4")]}  # Shares (ES1->SW1) with A
]
NUM_STREAMS = len(STREAMS)

# ==========================================
# Phase 2: Generate (Genetic Algorithm via DEAP)
# ==========================================
creator.create("FitnessMin", base.Fitness, weights=(-1.0,))
creator.create("Individual", list, fitness=creator.FitnessMin)

toolbox = base.Toolbox()
# Start times (injection at the source node) between 0 and 15
toolbox.register("attr_int", random.randint, 0, 15)
toolbox.register("individual", tools.initRepeat, creator.Individual, toolbox.attr_int, n=NUM_STREAMS)
toolbox.register("population", tools.initRepeat, list, toolbox.individual)

def get_link_schedules(individual):
    """ Helper to calculate arrival times on each link based on source injection time """
    link_schedules = {} # Format: { link: [ (start_time, end_time, stream_id) ] }
    
    for i, s in enumerate(STREAMS):
        current_time = individual[i]
        for link in s["path"]:
            if link not in link_schedules:
                link_schedules[link] = []
            
            start_on_link = current_time
            end_on_link = start_on_link + s["duration"]
            link_schedules[link].append((start_on_link, end_on_link, s["id"]))
            
            # Store-and-forward delay for the next link
            current_time = end_on_link + G.edges[link]["delay"]
            
    return link_schedules

def evaluate(individual):
    penalty = 0
    link_schedules = get_link_schedules(individual)
    
    # 1. Deadline penalty (Check End-to-End delay)
    for i, s in enumerate(STREAMS):
        # Calculate time when it finishes the last link
        last_link = s["path"][-1]
        finish_time = next(end for start, end, sid in link_schedules[last_link] if sid == s["id"])
        
        if finish_time > s["deadline"]:
            penalty += (finish_time - s["deadline"]) * 10 
            
    # 2. Collision penalty (Check overlaps PER LINK)
    for link, schedules in link_schedules.items():
        n = len(schedules)
        for i in range(n):
            for j in range(i + 1, n):
                start1, end1, _ = schedules[i]
                start2, end2, _ = schedules[j]
                # Overlap check on the SAME physical link
                if not (end1 <= start2 or end2 <= start1):
                    overlap = min(end1, end2) - max(start1, start2)
                    penalty += overlap * 20
                
    return (penalty,)

toolbox.register("evaluate", evaluate)
toolbox.register("mate", tools.cxTwoPoint)
toolbox.register("mutate", tools.mutUniformInt, low=0, up=15, indpb=0.2)
toolbox.register("select", tools.selTournament, tournsize=3)

def generate_schedule_ga():
    print("--- Phase 2: Generating Schedule (GA) ---")
    pop = toolbox.population(n=30)
    hof = tools.HallOfFame(1)
    stats = tools.Statistics(lambda ind: ind.fitness.values)
    stats.register("min", np.min)
    
    algorithms.eaSimple(pop, toolbox, cxpb=0.5, mutpb=0.2, ngen=15, stats=stats, halloffame=hof, verbose=False)
    
    best_ind = hof[0]
    schedule = {STREAMS[i]["id"]: best_ind[i] for i in range(NUM_STREAMS)}
    
    for s in STREAMS:
        print(f"GA Injection -> {s['id']}: Source Start at {schedule[s['id']]}")
    
    return schedule

# ==========================================
# Phase 3: Verify (Formal Verification using Z3)
# ==========================================
def verify_schedule(streams, schedule):
    print("\n--- Phase 3: Formal Verification with Z3 ---")
    solver = z3.Solver()
    
    # Variables for injection time at source
    inj_times = {s["id"]: z3.Int(f"inj_{s['id']}") for s in streams}
    
    for i, s1 in enumerate(streams):
        solver.add(inj_times[s1["id"]] >= 0)
        
        # Calculate symbolic end time for deadline check
        current_sym_time = inj_times[s1["id"]]
        for link in s1["path"]:
            current_sym_time = current_sym_time + s1["duration"] + G.edges[link]["delay"]
        solver.add(current_sym_time - G.edges[s1["path"][-1]]["delay"] <= s1["deadline"])

    # Link constraints (No overlap on shared links)
    for i, s1 in enumerate(streams):
        for j, s2 in enumerate(streams):
            if i < j:
                # Find shared links
                shared_links = set(s1["path"]).intersection(set(s2["path"]))
                for link in shared_links:
                    # Calculate arrival time at this specific link
                    t1 = inj_times[s1["id"]]
                    for l1 in s1["path"]:
                        if l1 == link: break
                        t1 = t1 + s1["duration"] + G.edges[l1]["delay"]
                        
                    t2 = inj_times[s2["id"]]
                    for l2 in s2["path"]:
                        if l2 == link: break
                        t2 = t2 + s2["duration"] + G.edges[l2]["delay"]
                    
                    # Mutual exclusion on the shared link
                    solver.add(z3.Or(t1 + s1["duration"] <= t2, t2 + s2["duration"] <= t1))
                
    # Bind Z3 variables to GA proposed schedule
    assumptions = [inj_times[s["id"]] == schedule[s["id"]] for s in streams]
    
    if solver.check(assumptions) == z3.sat:
        print("✅ VERIFIED: The GA schedule is mathematically VALID across the network.")
        return True
    else:
        print("❌ FAILED: The GA schedule violates network constraints.")
        return False

# ==========================================
# Phase 4: OMNeT++ Export (NeSTiNg GCL XML)
# ==========================================
def export_to_omnet(individual_schedule):
    print("\n--- Phase 4: Exporting GCL to OMNeT++ XML ---")
    
    # Get the actual schedule per link using our helper function
    ordered_list = [individual_schedule[s["id"]] for s in STREAMS]
    link_schedules = get_link_schedules(ordered_list)
    
    root = ET.Element("schedule")
    
    for link, schedules in link_schedules.items():
        switch_port = ET.SubElement(root, "port", name=f"{link[0]}_to_{link[1]}")
        
        # Sort by start time on this link
        schedules.sort(key=lambda x: x[0])
        
        for start_time, end_time, stream_id in schedules:
            entry = ET.SubElement(switch_port, "entry")
            entry.set("stream", stream_id)
            entry.set("start_time", str(start_time))
            entry.set("duration", str(end_time - start_time))
            
    xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="    ")
    
    with open("omnet_gcl.xml", "w") as f:
        f.write(xml_str)
        
    print("✅ Exported schedule to 'omnet_gcl.xml'")

if __name__ == "__main__":
    max_loops = 5
    for loop in range(1, max_loops + 1):
        print(f"\n========== GA-VERIFY LOOP {loop} ==========")
        proposed = generate_schedule_ga()
        if verify_schedule(STREAMS, proposed):
            export_to_omnet(proposed)
            break
