# Import a couple of very common python modules
import logging, time

# Import the CommPortal Python Library module
from commportal import CommPortal

import xbmc, xbmcgui, xbmcaddon
# import pprint

settings = xbmcaddon.Addon( id="script.advanced.commportal" )

COMMPORTAL_SERVER_NAME = settings.getSetting("serverName")
COMMPORTAL_CUST_PATH = settings.getSetting("customizationPath")
COMMPORTAL_DN = settings.getSetting("phoneNumber")
COMMPORTAL_PASSWORD = settings.getSetting("password")

def start():
    cp = CommPortal(COMMPORTAL_SERVER_NAME, COMMPORTAL_CUST_PATH, legacyMode=True)
    cp.login(COMMPORTAL_DN, COMMPORTAL_PASSWORD, wait=60)
    xbmc.executebuiltin('XBMC.Notification("CommPortal", "Logged In", 5000)')
    cp.registerForMessageNotifications(messagesCallback)
    # result = cp.downloadMessageCounts()
    # pp = pprint.PrettyPrinter(indent=4)
    # xbmc.executebuiltin('XBMC.Notification("CommPortal", '+ pp.pprint(result)+', 5000)')

def main(isAutostart=False):
    print 'script.advanced.commportal: Starting CommPortal script'
    
    print 'script.advanced.commportal: Closing CommPortal script'

def messagesCallback(commportal, event):
    # Let's download the complete list of messages
    messages = commportal.downloadMessageList()
    if messages:
        # Now, let's output some details about the first message in the list.
        if len(messages) > 0:
            message = messages[0]
            messageString = "Received: " + message['Received']['_'] + "\n"
            # The From field isn't present if the caller withheld their
            # number.
            if message.has_key('From'):
                messageString += "  From: " + message['From']['_']
            else:
                messageString += "  From: anonymous"
                
            xbmc.executebuiltin('XBMC.Notification("New Voicemail Recieved", ' + messageString + ', 10000)')     

if __name__ == '__main__':
    main()