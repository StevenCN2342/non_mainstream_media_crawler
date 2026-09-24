
class MediaDetector:


    def detect(self, html):


        text = html.lower()



        if "wp-content" in text:

            return "wordpress"



        if "ghost" in text:

            return "ghost"



        return "generic"

