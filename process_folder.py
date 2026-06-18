import os
import sys
import requests
import time

API_BASE_URL = "http://localhost:8000"

def process_folder(folder_path: str, company_id: str, period: str):
    if not os.path.isdir(folder_path):
        print(f"Error: {folder_path} is not a valid directory.")
        sys.exit(1)

    files_in_dir = [f for f in os.listdir(folder_path) if os.path.isfile(os.path.join(folder_path, f))]
    
    if not files_in_dir:
        print(f"No files found in {folder_path}.")
        sys.exit(1)

    print(f"Found {len(files_in_dir)} files in {folder_path}. Starting processing...\n")

    # Step 1 & 2: Upload and Extract for each file
    for filename in files_in_dir:
        file_path = os.path.join(folder_path, filename)
        print(f"--- Processing {filename} ---")
        
        # 1. Upload
        print(f"Uploading...")
        try:
            with open(file_path, 'rb') as f:
                response = requests.post(
                    f"{API_BASE_URL}/documents/upload",
                    files={"file": (filename, f)},
                    data={"company_id": company_id, "period": period}
                )
            response.raise_for_status()
            upload_data = response.json()
            document_id = upload_data.get("document_id")
            print(f"  [+] Upload successful! Document ID: {document_id}")
        except Exception as e:
            print(f"  [-] Upload failed for {filename}: {e}")
            continue

        # 2. Extract
        print(f"Extracting facts...")
        try:
            extract_payload = {
                "document_id": document_id,
                "company_id": company_id,
                "period": period
            }
            response = requests.post(
                f"{API_BASE_URL}/kpis/extract",
                json=extract_payload
            )
            response.raise_for_status()
            extract_data = response.json()
            print(f"  [+] Extraction successful! Facts Extracted: {extract_data.get('facts_extracted')}, Validated: {extract_data.get('facts_validated')}")
        except Exception as e:
            print(f"  [-] Extraction failed for {filename}: {e}")
            if response is not None:
                print(f"      Response: {response.text}")
            continue

        print() # Empty line for readability
        time.sleep(1) # Slight pause to not overwhelm the server

    # Step 3: Calculate KPIs
    print(f"--- Calculating KPIs for {company_id} ({period}) ---")
    try:
        calc_payload = {
            "company_id": company_id,
            "period": period,
            "kpi_ids": None # None means calculate all available
        }
        response = requests.post(
            f"{API_BASE_URL}/kpis/calculate",
            json=calc_payload
        )
        response.raise_for_status()
        calc_data = response.json()
        
        results = calc_data.get("results", [])
        calculated = [r for r in results if r.get("status") in ["calculated", "calculated_derived"]]
        
        print(f"[+] Calculation complete! Calculated {len(calculated)} out of {len(results)} total KPIs successfully.")
    except Exception as e:
        print(f"[-] KPI Calculation failed: {e}")
        if response is not None:
            print(f"    Response: {response.text}")

if __name__ == "__main__":
    if len(sys.argv) != 4:
        print("Usage: python process_folder.py <folder_path> <company_id> <period>")
        print("Example: python process_folder.py ./sample_data/ novamind 2025-2026")
        sys.exit(1)

    folder_path_arg = sys.argv[1]
    company_id_arg = sys.argv[2]
    period_arg = sys.argv[3]

    process_folder(folder_path_arg, company_id_arg, period_arg)
