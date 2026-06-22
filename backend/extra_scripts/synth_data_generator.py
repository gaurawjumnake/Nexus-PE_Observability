import pandas as pd
import os
import json

# with open("novamind_kpis_export.json" ,"r") as f:
#     data = json.load(f)

# kpi_list = [item['kpi_id'] for item in data ]
# print(len(kpi_list))

import numpy as np
import pandas as pd
from scipy import stats
from faker import Faker
from datetime import datetime
import calendar

# Reasonable (low, high) range per known metric. Unknown columns default to (0, 100).
COLUMN_RANGES = {
    'active_ai_users': (100, 5000), 'adoption_velocity_rank': (1, 50), 'adoption_yoy_growth': (5, 60),
    'ai_assisted_revenue': (10000, 500000), 'ai_governance_score': (20, 95), 'ai_incident_rate': (0, 10),
    'ai_maturity_score': (10, 90), 'ai_revenue': (50000, 2000000), 'ai_roi': (-20, 300),
    'api_licensing_spend': (5000, 100000), 'api_success_rate': (85, 100), 'availability_uptime': (95, 100),
    'budget_adherence': (70, 110), 'budget_variance': (-25, 25), 'cloud_spend': (10000, 500000),
    'company_maturity_rank': (1, 100), 'copilot_adoption': (0, 100), 'cost_efficiency_rank': (1, 80),
    'cost_per_outcome': (20, 500), 'cost_savings': (1000, 200000), 'critical_incident_count': (0, 15),
    'cross_sell_uplift': (0, 40), 'custom_agent_adoption': (0, 100), 'data_privacy_compliance': (80, 100),
    'dau_mau_intensity': (0.1, 0.7), 'direct_ai_revenue': (10000, 1000000), 'ebitda_uplift': (0, 15),
    'embedded_analytics_adoption': (0, 100), 'error_rate': (0, 12), 'fallback_rate': (0, 20),
    'forecasted_ai_spend': (10000, 600000), 'governance_maturity_score': (10, 90), 'governance_rank': (1, 60),
    'hallucination_rate': (0, 18), 'human_review_coverage': (10, 100), 'industry_benchmark_ratio': (0.5, 1.6),
    'mttr': (1, 36), 'p95_latency': (100, 2500), 'payback_period': (3, 30), 'percent_ai_in_production': (0, 100),
    'pipeline_influenced_revenue': (10000, 800000), 'policy_compliance_rate': (70, 100),
    'portfolio_ai_adoption_score': (10, 90), 'portfolio_benchmark_score': (10, 90), 'power_user_ratio': (0, 50),
    'production_ratio': (0, 100), 'productivity_gain': (0, 45), 'projects_in_poc': (0, 30),
    'projects_in_production': (0, 100), 'regulatory_readiness': (20, 95), 'spend_by_model_family': (5000, 300000),
    'stalled_projects': (0, 15), 'strategic_alignment_score': (10, 90), 'talent_readiness_score': (10, 90),
    'technical_maturity_score': (10, 90), 'top_quartile_position': (0, 100), 'total_ai_projects': (5, 150),
    'total_ai_spend': (50000, 3000000), 'vendor_compliance': (60, 100), 'approved_ai_budget ':(0,2000000)
}

# Columns that should be whole numbers
INTEGER_COLUMNS = {
    'active_ai_users', 'adoption_velocity_rank', 'company_maturity_rank', 'cost_efficiency_rank',
    'governance_rank', 'critical_incident_count', 'projects_in_poc', 'projects_in_production',
    'stalled_projects', 'total_ai_projects', 'p95_latency',
}


def generate_synthetic_data(company_name, columns, year, seed=None):
    Faker.seed(seed if seed is not None else abs(hash(company_name)) % (2**32))
    fake = Faker()
    rng = np.random.default_rng(seed if seed is not None else fake.random_int(0, 2**32 - 1))

    all_dates = []
    all_data = {col: [] for col in columns}
    
    # Iterate through all 12 months
    for month in range(1, 13):
        # Get the number of days in this month
        num_days_in_month = calendar.monthrange(year, month)[1]
        first_day = datetime(year, month, 1)
        
        # Randomly select 20-30 dates in this month
        num_entries = rng.integers(20, 31)  # 20-30 inclusive
        random_days = sorted(rng.choice(range(1, num_days_in_month + 1), size=num_entries, replace=True))
        
        # Generate dates for this month
        month_dates = [datetime(year, month, day) for day in random_days]
        all_dates.extend(month_dates)
        
        # Generate metric values with slight trend across the year
        # (earlier months slightly lower, later months slightly higher)
        month_progress = month / 12
        
        for col in columns:
            low, high = COLUMN_RANGES.get(col, (0, 101))
            
            # Add a slight trend across the year
            base_value = low + (high - low) * month_progress * 0.3
            
            # Generate values for each entry in this month
            for _ in range(num_entries):
                noise = stats.norm.rvs(loc=0, scale=(high - low) * 0.08, random_state=rng)
                value = np.clip(base_value + noise, low, high)
                
                if col in INTEGER_COLUMNS:
                    value = int(np.round(value))
                else:
                    value = round(value, 2)
                
                all_data[col].append(value)
    
    # Create DataFrame
    df = pd.DataFrame({'date': all_dates})
    for col in columns:
        df[col] = all_data[col]
    
    # Sort by date
    df = df.sort_values('date').reset_index(drop=True)
    
    return df


def save_data(df, company_name, year, out_format='both', outdir='.'):
    base = f"{outdir}/{company_name.replace(' ', '_')}_{year}"
    
    # if out_format in ('csv', 'both'):
    #     filepath = f"{base}.csv"
    #     df.to_csv(filepath, index=False)
    #     print(f"  ✓ Saved CSV: {filepath}")
    
    if out_format in ('xlsx', 'both'):
        filepath = f"{base}.xlsx"
        df.to_excel(filepath, index=False)
        print(f"  ✓ Saved Excel: {filepath}")


def main(company_name, year, seed=None, outdir='.'):

    columns = [
        'active_ai_users', 'adoption_velocity_rank', 'adoption_yoy_growth', 'ai_assisted_revenue',
        'ai_governance_score', 'ai_incident_rate', 'ai_maturity_score', 'ai_revenue', 'ai_roi',
        'api_licensing_spend', 'api_success_rate', 'availability_uptime', 'budget_adherence',
        'budget_variance', 'cloud_spend', 'company_maturity_rank', 'copilot_adoption',
        'cost_efficiency_rank', 'cost_per_outcome', 'cost_savings', 'critical_incident_count',
        'cross_sell_uplift', 'custom_agent_adoption', 'data_privacy_compliance', 'dau_mau_intensity',
        'direct_ai_revenue', 'ebitda_uplift', 'embedded_analytics_adoption', 'error_rate', 'fallback_rate',
        'forecasted_ai_spend', 'governance_maturity_score', 'governance_rank', 'hallucination_rate',
        'human_review_coverage', 'industry_benchmark_ratio', 'mttr', 'p95_latency', 'payback_period',
        'percent_ai_in_production', 'pipeline_influenced_revenue', 'policy_compliance_rate',
        'portfolio_ai_adoption_score', 'portfolio_benchmark_score', 'power_user_ratio', 'production_ratio',
        'productivity_gain', 'projects_in_poc', 'projects_in_production', 'regulatory_readiness',
        'spend_by_model_family', 'stalled_projects', 'strategic_alignment_score', 'talent_readiness_score',
        'technical_maturity_score', 'top_quartile_position', 'total_ai_projects', 'total_ai_spend', 'vendor_compliance', 'approved_ai_budget' 
    ]
    
    print(f"SYNTHETIC DATA GENERATOR - DAILY ENTRIES")
    print(f"\nCompany: {company_name}")
    print(f"Year: {year}")
    print(f"Columns: {len(columns)}")
    print(f"Entries per month: 20-30 (random)")
    
    # Generate data
    print(f"\nGenerating synthetic data...")
    df = generate_synthetic_data(company_name, columns, year=year)
    
    # Display summary
    print(f"\n" + "-" * 80)
    print(f"SUMMARY")
    print("-" * 80)
    print(f"✓ Total rows: {len(df)}")
    print(f"✓ Date range: {df['date'].min().date()} to {df['date'].max().date()}")
    print(f"✓ Columns: {len(df.columns)}")
    
    # Breakdown by month
    print(f"\nBreakdown by month:")
    monthly_counts = df['date'].dt.to_period('M').value_counts().sort_index()
    for period, count in monthly_counts.items():
        print(f"  {period}: {count:2d} entries")
    
    # Save data
    print(f"\nSaving data...")
    save_data(df, company_name, year, out_format='both', outdir=outdir)
    

    
    return df


if __name__ == "__main__":
    company_name = "Provation"
    year = 2026
    # seed = 42 
    df = main(company_name, year, outdir='.')
