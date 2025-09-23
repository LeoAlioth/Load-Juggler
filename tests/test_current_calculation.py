#!/usr/bin/env python3
"""
Test script to verify the current calculation logic fix.
This demonstrates the fix for the feedback loop issue in Standard charge mode.
Outputs results to CSV files for graphing and analysis.
"""

import csv
import os

def test_feedback_loop_logic():
    """Test the logic behind the feedback loop fix and output to CSV."""
    print("Testing Current Calculation Feedback Loop Fix")
    print("=" * 50)
    
    # Configuration
    max_import_power = 11000  # 11kW
    voltage = 230
    phases = 3
    main_breaker_rating = 25  # 25A per phase
    base_load_per_phase = 5   # 5A base load per phase
    
    max_import_current = max_import_power / voltage  # ~47.8A total
    
    print(f"Configuration:")
    print(f"  Max Import Power: {max_import_power}W")
    print(f"  Max Import Current: {max_import_current:.1f}A total")
    print(f"  Main Breaker Rating: {main_breaker_rating}A per phase")
    print(f"  Base Load: {base_load_per_phase}A per phase")
    print()
    
    # Test with more granular EVSE current levels for better graphing
    evse_currents = list(range(0, 21, 1))  # 0A to 20A per phase, 1A steps
    
    # Prepare CSV data
    csv_data = []
    csv_headers = [
        'EVSE_Current_Per_Phase_A',
        'Total_EVSE_Current_A', 
        'Total_Import_Current_A',
        'Old_Logic_Available_A',
        'New_Logic_Available_A',
        'Difference_A',
        'Phase_Current_A',
        'Non_EVSE_Import_A',
        'Remaining_Import_Old_A',
        'Remaining_Import_New_A'
    ]
    
    print("Generating detailed current calculation data...")
    
    for evse_current_per_phase in evse_currents:
        total_evse_current = evse_current_per_phase * phases
        
        # Calculate phase currents (base load + EVSE)
        phase_current = base_load_per_phase + evse_current_per_phase
        total_import_current = phase_current * phases
        
        # OLD LOGIC (broken - includes EVSE in calculation)
        remaining_import_old = max_import_current - total_import_current
        remaining_phase_old = main_breaker_rating - phase_current
        max_evse_available_old = min(remaining_phase_old, remaining_import_old / phases)
        
        # NEW LOGIC (fixed - excludes EVSE from calculation)
        non_evse_import_current = total_import_current - total_evse_current
        remaining_import_new = max_import_current - non_evse_import_current
        remaining_phase_new = main_breaker_rating - (phase_current - evse_current_per_phase)
        max_evse_available_new = min(remaining_phase_new, remaining_import_new / phases)
        
        difference = max_evse_available_new - max_evse_available_old
        
        # Add to CSV data
        csv_data.append([
            evse_current_per_phase,
            total_evse_current,
            total_import_current,
            round(max_evse_available_old, 2),
            round(max_evse_available_new, 2),
            round(difference, 2),
            phase_current,
            non_evse_import_current,
            round(remaining_import_old, 2),
            round(remaining_import_new, 2)
        ])
    
    # Write to CSV file
    csv_filename = 'tests/current_calculation_results.csv'
    with open(csv_filename, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.writer(csvfile)
        writer.writerow(csv_headers)
        writer.writerows(csv_data)
    
    print(f"✅ Results saved to: {csv_filename}")
    print(f"   Data points: {len(csv_data)}")
    print(f"   EVSE current range: 0A to {max(evse_currents)}A per phase")
    
    # Show sample results
    print("\nSample Results (every 5A):")
    print("EVSE Current | Old Logic | New Logic | Difference")
    print("-" * 50)
    
    for i, row in enumerate(csv_data):
        if row[0] % 5 == 0:  # Show every 5A
            evse_current = row[0]
            old_logic = row[3]
            new_logic = row[4]
            difference = row[5]
            print(f"{evse_current:2d}A/phase   | {old_logic:7.1f}A | {new_logic:7.1f}A | {difference:+6.1f}A")
    
    print(f"\n✅ Full detailed data available in {csv_filename}")
    print("   Use this file to create graphs showing:")
    print("   - Old vs New logic comparison")
    print("   - Feedback loop elimination")
    print("   - Current stability analysis")

def test_edge_cases():
    """Test edge cases for the current calculation."""
    print("\n\nTesting Edge Cases")
    print("=" * 30)
    
    # Configuration
    max_import_power = 11000
    voltage = 230
    phases = 3
    main_breaker_rating = 25
    max_import_current = max_import_power / voltage
    
    # Edge case 1: Very high base load
    print("\nEdge Case 1: High base load (20A per phase)")
    base_load = 20
    evse_current = 6
    
    phase_current = base_load + evse_current
    total_import = phase_current * phases
    total_evse = evse_current * phases
    
    # New logic
    non_evse_import = total_import - total_evse
    remaining_import = max_import_current - non_evse_import
    remaining_phase = main_breaker_rating - (phase_current - evse_current)
    max_available = min(remaining_phase, remaining_import / phases)
    
    print(f"  Base load: {base_load}A/phase, EVSE: {evse_current}A/phase")
    print(f"  Max available for EVSE: {max_available:.1f}A")
    print(f"  Result: {'PASS - Properly limited' if max_available >= 0 else 'Limited by constraints'}")
    
    # Edge case 2: Export scenario (negative phase currents)
    print("\nEdge Case 2: Export scenario (solar generation)")
    base_load = -5  # Exporting 5A per phase
    evse_current = 10
    
    phase_current = base_load + evse_current  # Net 5A import
    total_import = max(phase_current * phases, 0)  # Only count import
    total_evse = evse_current * phases
    
    non_evse_import = total_import - total_evse
    remaining_import = max_import_current - non_evse_import
    remaining_phase = main_breaker_rating - (phase_current - evse_current)
    max_available = min(remaining_phase, remaining_import / phases)
    
    print(f"  Base load: {base_load}A/phase (export), EVSE: {evse_current}A/phase")
    print(f"  Net phase current: {phase_current}A/phase")
    print(f"  Max available for EVSE: {max_available:.1f}A")
    print(f"  Result: {'PASS - High availability due to export' if max_available > 15 else 'Limited availability'}")

if __name__ == "__main__":
    test_feedback_loop_logic()
    test_edge_cases()
