import requests
from bs4 import BeautifulSoup

def scrape_jobs(url):
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')
    # Simple scraper logic
    return soup.title.string

if __name__ == "__main__":
    print(scrape_jobs("https://example.com"))
