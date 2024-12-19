import streamlit as st
from playwright.sync_api import sync_playwright
from python_anticaptcha import AnticaptchaClient, ImageToTextTask
import pandas as pd
import time
import os
from datetime import datetime

# Install Playwright browsers (required for deployment)
os.system("playwright install chromium")

# Streamlit App Title
st.title("Clark County Probate Court Records Scraper")

# Add configuration fields
st.subheader("Configuration")
api_key = st.text_input("Enter Anticaptcha API Key", value="c559d724e9053a460ff51a84ca669714", type="password")

# Add date picker
st.subheader("Select Search Date")
search_date = st.date_input(
    "Choose a date",
    min_value=datetime(1978, 1, 1),
    max_value=datetime.now()
)

# Add a button to start the scraping process
run_button = st.button("Start Scraping")

def parse_city_state_zip(location_string):
    """Parse a city/state/zip string into components"""
    try:
        # Expected format: "Fairborn, Oh 45324"
        parts = location_string.split(',')
        city = parts[0].strip()
        # Split state and zip
        state_zip = parts[1].strip().split(' ')
        state = state_zip[0].strip()
        zip_code = state_zip[1].strip() if len(state_zip) > 1 else ""
        return city, state, zip_code
    except:
        return "", "", ""

from io import BytesIO

def solve_captcha_from_element(page, api_key):
    """Solve captcha by capturing the current image from the page"""
    try:
        # Wait for captcha image and capture it
        image_element = page.locator('#captchaImage')
        # Screenshot the specific captcha element
        image_bytes = image_element.screenshot()
        
        # Convert bytes to file-like object
        image_file = BytesIO(image_bytes)
        image_file.seek(0)  # Ensure we're at the start of the stream
        
        # Create anticaptcha client and task
        client = AnticaptchaClient(api_key)
        task = ImageToTextTask(image_file)
        job = client.createTask(task)
        job.join()
        return job.get_captcha_text()
    except Exception as e:
        st.error(f"Error solving captcha: {e}")
        return None

def handle_captcha(page):
    """Handle captcha solving and input"""
    try:
        # Wait for the CAPTCHA form to be present
        st.info("Waiting for CAPTCHA...")
        page.wait_for_selector('#captchaResponse')
        page.wait_for_selector('#captchaImage')
        
        # Solve the captcha using the actual displayed image
        st.info("Solving CAPTCHA...")
        captcha_text = solve_captcha_from_element(page, api_key)
        
        if captcha_text:
            # Find the captcha input field and enter text
            captcha_input = page.locator('#captchaResponse')
            captcha_input.fill(captcha_text)
            st.success(f"CAPTCHA solved: {captcha_text}")
            
            # Short wait to ensure the input is registered
            time.sleep(1)
            return True
        
        return False
    except Exception as e:
        st.error(f"Error handling captcha: {e}")
        return False

def extract_table_data(page, section_type):
    """Extract data from a specific section"""
    return page.evaluate(f'''() => {{
        const data = {{}};
        
        // Find the section by its header
        const header = Array.from(document.querySelectorAll('h4.search')).find(h => h.textContent.includes('{section_type}'));
        if (!header) return data;
        
        // Get the table following this header
        const table = header.closest('tr').nextElementSibling.querySelector('table');
        if (!table) return data;
        
        // Process all rows in the table
        const rows = table.querySelectorAll('tr');
        rows.forEach(row => {{
            const label = row.querySelector('th.column1')?.textContent?.trim();
            if (!label) return;
            
            const value = row.querySelector('td.column2')?.textContent?.trim() || '';
            const value2 = row.querySelector('td.column4')?.textContent?.trim() || '';
            
            // For Decedent section, rename address fields
            if ('{section_type}' === 'Decedent') {{
                if (label.includes('Address')) {{
                    data['Property_Address'] = value;
                }} else if (label.includes('City/State/ZIP')) {{
                    data['Property_Location'] = value;  // We'll split this later
                }} else if (value2) {{
                    data[label.replace(':', '') + '_Attorney'] = value2;
                }} else {{
                    data[label.replace(':', '')] = value;
                }}
            }} else {{
                // For Fiduciary section
                if (label.includes('City/State/ZIP')) {{
                    data['Mailing_Location'] = value;  // We'll split this later
                }} else if (value) {{
                    data[label.replace(':', '')] = value;
                }}
                if (value2) data[label.replace(':', '') + '_column4'] = value2;
            }}
        }});
        
        return data;
    }}''')

def navigate_and_scrape(playwright, search_date):
    """Navigate to the search page and scrape data."""
    browser = playwright.chromium.launch(headless=True)
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
    )
    
    try:
        page = context.new_page()
        
        # Initial navigation
        st.info("Navigating to the records search page...")
        page.goto("https://probate.clarkcountyohio.gov/recordSearch.php")
        page.wait_for_load_state('networkidle')
        
        # Click continue link to get to search form
        st.info("Accepting the agreement...")
        continue_link = page.get_by_role("link", name="Continue")
        continue_link.click()
        page.wait_for_load_state('networkidle')
        time.sleep(2)
        
        # Fill date fields
        st.info("Filling in the date fields...")
        page.locator('#searchFMonth').select_option(str(search_date.month))
        page.locator('#searchFDay').select_option(str(search_date.day))
        page.locator('#searchFYear').select_option(str(search_date.year))
        
        # Uncheck case type boxes
        st.info("Unchecking case type boxes...")
        case_types = ['PC', 'PG', 'PR', 'PM', 'PT']
        for case_type in case_types:
            checkbox = page.locator(f'#checkCaseType-{case_type}')
            if checkbox.is_checked():
                checkbox.uncheck()
                time.sleep(0.5)
        
        # Handle captcha and submit search
        if handle_captcha(page):
            submit_button = page.locator('#buttonSubmit')
            submit_button.click()
            page.wait_for_load_state('networkidle')
            
            # Process search results
            st.info("Processing search results...")
            case_links = page.locator('a.caseLink')
            total_cases = case_links.count()
            st.info(f"Found {total_cases} cases to process")
            
            all_case_data = []
            progress_bar = st.progress(0)
            
            # Process each case
            for i in range(total_cases):
                # Store the current URL and click the case link
                current_case = case_links.nth(i)
                current_url = page.url
                
                current_case.click()
                page.wait_for_url(lambda url: url != current_url, timeout=60000)
                
                # Get case status
                status_elem = page.locator('.alert-danger strong')
                case_status = status_elem.inner_text() if status_elem.count() > 0 else ""
                
                # Extract data using JavaScript evaluation
                decedent_data = extract_table_data(page, 'Decedent')
                fiduciary_data = extract_table_data(page, 'Fiduciary')
                case_info = extract_table_data(page, 'Case Information')
                
                # Combine all data
                case_data = {
                    'case_status': case_status,
                    **decedent_data,
                    **fiduciary_data,
                    **case_info
                }
                
                # Split property location into components
                if 'Property_Location' in case_data:
                    city, state, zip_code = parse_city_state_zip(case_data['Property_Location'])
                    case_data['Property_City'] = city
                    case_data['Property_State'] = state
                    case_data['Property_ZIP'] = zip_code
                    del case_data['Property_Location']
                
                # Split mailing location into components
                if 'Mailing_Location' in case_data:
                    city, state, zip_code = parse_city_state_zip(case_data['Mailing_Location'])
                    case_data['Mailing_City'] = city
                    case_data['Mailing_State'] = state
                    case_data['Mailing_ZIP'] = zip_code
                    del case_data['Mailing_Location']
                
                all_case_data.append(case_data)
                
                # Go back to results page
                page.go_back()
                page.wait_for_load_state('networkidle')
                
                # Update progress
                progress_bar.progress((i + 1) / total_cases)
            
            # Create DataFrame and enable download
            if all_case_data:
                df = pd.DataFrame(all_case_data)
                st.success("Scraping completed successfully!")
                st.dataframe(df)
                
                # Download button
                st.download_button(
                    label="Download CSV",
                    data=df.to_csv(index=False).encode('utf-8'),
                    file_name=f'probate_records_{search_date.strftime("%Y%m%d")}.csv',
                    mime='text/csv'
                )
            
            return browser, page
            
    except Exception as e:
        st.error(f"An error occurred: {e}")
        browser.close()
        return None, None

# Run the scraper when the button is clicked
if run_button:
    st.info(f"Starting the scraping process for date: {search_date}")
    with sync_playwright() as playwright:
        browser, page = navigate_and_scrape(playwright, search_date)
        if browser:
            time.sleep(5)
            browser.close()