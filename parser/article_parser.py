
from bs4 import BeautifulSoup



class ArticleParser:


    def parse(self, html, url):


        soup = BeautifulSoup(

            html,

            "lxml"

        )


        paragraphs = []


        for p in soup.find_all("p"):

            paragraphs.append(

                p.get_text(

                    strip=True

                )

            )



        return {


            "url": url,


            "content":

            "\n".join(paragraphs)


        }

