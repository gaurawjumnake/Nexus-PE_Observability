import json
import argparse
from pathlib import Path
from typing import Optional

def convert_to_markdown(data: list) -> str:
    markdown_output = []

    for doc in data:
        filename = doc.get('filename', 'Unknown Document')
        file_type = doc.get('file_type', 'N/A')
        total_pages = doc.get('total_pages', 'N/A')

        markdown_output.append(f"# Document: {filename}\n")
        markdown_output.append(f"**Source File Type:** `{file_type}`\n")
        markdown_output.append(f"**Total Pages:** {total_pages}\n")
        
        for page in doc.get('pages', []):
            page_num = page.get('page_number', 'N/A')
            markdown_output.append(f"\n---\n\n## Page {page_num}\n")
            text_content = page.get('text_content', '').strip()
            
            if text_content:
                markdown_output.append(text_content)
                markdown_output.append("\n")

            images = page.get('images', [])
            if images:
                if 'image data' not in text_content.lower() and 'image of' not in text_content.lower():
                    markdown_output.append("\n### Image Descriptions\n")
                
                for img in images:
                    img_desc = img.get('description', 'No description available.')
                    if img_desc not in text_content:
                         markdown_output.append(f"- {img_desc}")
                markdown_output.append("\n")

    final_output = "\n".join(markdown_output)
    return final_output.replace('\n\n\n', '\n\n')

def json_to_md_main(json_data: list, base_file_name: Optional[str] = None) -> str:
    """
    Converts JSON data (list of parsed document dicts) to a single Markdown string.
    Optionally, it can still save to a .md file if base_file_name is provided,
    but its primary purpose here is to return the string.
    """
    markdown_content = convert_to_markdown(json_data)

    if base_file_name:
        md_file_path = f"{base_file_name}.md"
        print(f"(Optional) Writing Markdown to: {md_file_path}")
        try:
            with open(md_file_path, 'w', encoding='utf-8') as f:
                f.write(markdown_content)
            print(f"Successfully wrote to {md_file_path}")
        except Exception as e:
            print(f"Error writing markdown to file {md_file_path}: {e}")

    return markdown_content


if __name__ == "__main__":
    current_dir = Path(__file__).resolve().parent
    example_json_file_path = current_dir / 'SOB_parsed_results.json'
    output_base_name = current_dir / 'SOB_parsed_results'

    try:
        print(f"Reading example JSON from: {example_json_file_path}")
        with open(example_json_file_path, 'r', encoding='utf-8') as f:
            sample_json_data = json.load(f)
        
        print("Converting JSON data to Markdown content...")
        markdown_output = json_to_md_main(json_data=sample_json_data, base_file_name=output_base_name) # type:ignore
        
        print("\n--- Markdown Output (first 500 chars) ---")
        print(markdown_output[:500] + "...")
        print("\nMarkdown conversion test complete!")

    except FileNotFoundError:
        print(f"Error: Test JSON file not found at {example_json_file_path}. Please create it or update the path.")
    except json.JSONDecodeError:
        print(f"Error: Could not decode JSON from {example_json_file_path}. Please ensure it's valid JSON.")
    except Exception as e:
        print(f"An unexpected error occurred: {e}")