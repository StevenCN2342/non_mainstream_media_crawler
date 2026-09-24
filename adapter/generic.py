
from bs4 import BeautifulSoup



class GenericAdapter:


    def parse(self, html, url):


        soup = BeautifulSoup(

            html,

            "lxml"

        )


        title = ""


        if soup.title:

            title = soup.title.text.strip()



        return {

            "url": url,

            "title": title,

            "content": ""

        }

