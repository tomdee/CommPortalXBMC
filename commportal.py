"""
  CommPortal Python Library
 
  Copyright (c) 2009 Metaswitch Networks

  Provides classes and methods to provide easy access for python applications
  to Metaswitch CommPortal.
  
  Version: 0.2.2
  Compatible with: MetaSphere EAS/CommPortal V7.0.
"""

import threading, logging, time, traceback, simplejson, urllib, urllib2
import httplib, socket
from functools import wraps

log = logging.getLogger("CommPortal Python Library")

# XXX Need to document callback method parameters somewhere

def EntryExit(f):
    """Decorator to wrap all methods and log their entry and exit points."""
    @wraps(f)
    def wrapper(*args, **kwds):
        log.debug("Entry: %s" % f.__name__)
        rc = f(*args, **kwds)
        log.debug("Exit: %s" % f.__name__)
        return rc
    return wrapper

# Main CommPortal Python Library class.           
class CommPortal(threading.Thread):
    """Main CommPortal Python Library class"""
    
    SCHEDULER_INTERVAL = 1
    
    HIGH_LEVEL_LOGGING = log.info
    LOW_LEVEL_LOGGING = log.debug
    
    CP_VERSION_STRING = '7.0'
    
    LOGGED_OUT = 1
    LOGGED_IN = 2

    INITIAL_LOGIN_RETRY_TIMER = 2
    MAX_LOGIN_RETRY_TIMER = 60
    KEEPALIVE_TIMER = 60
    
    MULTIPART_BOUNDARY = "AaB03x"
    
    class LoggedInError(ValueError):
        "Already logged into CommPortal"
        
    class NotLoggedInError(ValueError):
        "Not logged into CommPortal"    
        
    class LogInFailedError(ValueError):
        "Failed to log into CommPortal"
        
    class KeepAliveFailedError(ValueError):
        "CommPortal session dropped"  
        
    class UnsupportedModeError(ValueError):
        "Only legacy mode currently supported"
        
    @EntryExit    
    def __init__(self, serverName, custPath, https=True, legacyMode=False):
        """Class initialization routine
        
        serverName = CommPortal server domain name or IP address
        custPath = CommPortal server customization path
        https = Whether https should be uses (defaults to True)
        legacyMode = Whether to use pre-V7.1 interface or new V7.1 interface.
                     Only legacy mode (pre-V7.1 interface) is currently
                     supported, but once the V7.1 interface is supported this
                     will be the default.  For now you must pass 
                     legacyMode=True"""
        if not legacyMode:
            raise CommPortal.UnsupportedModeError
        threading.Thread.__init__(self)
        self._serverName = serverName
        self._custPath = custPath
        if self._custPath != None and len(self._custPath) > 0:
            if self._custPath[0] != '/':
                self._custPath = '/'+self._custPath
        self._https = https
        self._dn = None
        self._password = None
        self._loggedIn = False
        self._stopNow = False
        self._callback = None
        self._session = None
        self._logfn = CommPortal.HIGH_LEVEL_LOGGING
        self._reLogInNow = False
        self._retryTimer = CommPortal.INITIAL_LOGIN_RETRY_TIMER
        self._callbackThread = _CallbackThread(self)
        self._workThreads = []
        self._legacyMode = legacyMode
      
    @EntryExit    
    def login(self, dn, password, wait=None, callback=None):
        """Method to log into CommPortal.
        
        dn = Directory Number of subscriber
        password = Password of subscriber
        wait = Seconds to block waiting for login to complete.  Note login
               will continue to be attempted after this time if necessary.
        callback = Function to be called back with event parameter when login
                   has succeeded.  You musn't perform any blocking actions in
                   this callback method.
        
        Returns True if logged in or False if not (which may be beause you
        opted not to wait until it succeeded).
        
        The callback method supplied must take the following parameters:
        
        commportal = commportal object to allow the callback to call Library
                     methods
        event = Event which has occurred - either LOGGED_IN or LOGGED_OUT"""
        rc = False
        if self.isAlive():
            log.warning("Already logged into CommPortal")
            raise CommPortal.LoggedInError
        if self._callbackThread.isAlive():
            log.warning("Callback thread still running")
            raise CommPortal.LoggedInError
        self._dn = dn
        self._password = password
        self._callback = callback
        self._callbackThread.start()
        self.start()
        if wait:
            schedInt = CommPortal.SCHEDULER_INTERVAL
            ii = 0
            while (ii < wait):
                if schedInt > (wait - ii):
                    if (wait - ii) > 0:
                        schedInt = wait - ii
                    else:
                        break
                time.sleep(schedInt)
                ii += schedInt
                if self._loggedIn:
                    rc = True
                    break
        log.info("Logged in: " + str(rc))
        return rc
    
    @EntryExit    
    def logout(self, wait=None, callback=None):
        """Method to log out of CommPortal.
        
        wait = Seconds to block waiting for logout processing to complete.
               Note logout will continue to be attempted after this time if
               necessary.
        callback = Function to be called back with event parameter when login
                   has succeeded.  You musn't perform any blocking actions in
                   this callback method.
        
        Returns True if logged out or False if not (which may be because you
        opted not to wait until it had succeeded).

        The callback method supplied must take the following parameters:
        
        commportal = commportal object to allow the callback to call Library
                     methods
        event = Event which has occurred - either LOGGED_IN or LOGGED_OUT"""
        rc = False
        if not self._loggedIn or not self.isAlive():
            log.warning("Not logged into CommPortal")
            raise CommPortal.NotLoggedInError
        while len(self._workThreads) > 0:
            thread = self._workThreads.pop(0)
            thread.stop(wait=True)
        self._callback=callback
        self._stopNow = True
        if wait:
            schedInt = CommPortal.SCHEDULER_INTERVAL
            ii = 0
            while (ii < wait):
                if schedInt > (wait - ii):
                    if (wait - ii) > 0:
                        schedInt = wait - ii
                    else:
                        break
                time.sleep(schedInt)
                ii += schedInt
                if not self._loggedIn:
                    rc = True
                    break
        self._callbackThread.stop()
        if wait:
            schedInt = CommPortal.SCHEDULER_INTERVAL
            while (ii < wait):
                if schedInt > (wait - ii):
                    if (wait - ii) > 0:
                        schedInt = wait - ii
                    else:
                        break
                time.sleep(schedInt)
                ii += schedInt
                if not self._callbackThread.isAlive():
                    rc = True
                    break
        log.info("Logged out: " + str(rc))
        return rc
               
    @EntryExit
    def buildAndSendPost(self, postArgs, postPath, redirect=False, postType="ARGS"):
        """Method to build and send a post to CommPortal.
        
        postArgs = Dictionary of arguments to post to CommPortal
        postPath = Path (after /session/line) to post to
        redirect = Optional parameter to indicate redirect expected
        postType = ARGS RAW_JSON or MULTIPART_JSON depending on type of post
                   arguments.  Defaults to ARGS
        
        Returns (rc, session) where
        rc = True if successfully and False otherwise
        session = Error info if unsuccessful and session ID if successful"""
        log.debug("Post path is: " + postPath)
        log.debug("Cust path is: " + self._custPath)
        rc = False

        h1 = self._getConnection()
        if postType not in ["ARGS","RAW_JSON","MULTIPART_JSON"]:
            log.error("Invalid post type: " + str(postType))
            raise TypeError
        
        if postType == "ARGS":
            data = urllib.urlencode(postArgs)
        elif postType == "MULTIPART_JSON":
            data = postArgs    
        else:
            data = simplejson.dumps(postArgs)
        log.debug("Args are: " + data)
        headers = self._buildHttpHeaders(postType)

        try:
            # Need to add code to send session and line info when required
            h1.request('POST', self._custPath + postPath, data, headers)
            instance = h1.getresponse()
        except httplib.HTTPException, e:
            return rc, e
        except socket.timeout:
            return rc, 'Timed out.'
        except socket.error, e:
            return rc, 'Socket Error: '+ str(e)
        except Exception, e:
            return rc, 'Unknown error: ' + str(e)
        if not redirect and instance.status != 200:
            return rc, 'Error: %d %s' % (instance.status, instance.reason)
        elif redirect and instance.status != 302:
            return rc, 'Error: %d %s' % (instance.status, instance.reason)
        
        # Check if we got an error code and, if so, return it
        if redirect:    
            if instance.getheader('location').find('error') != -1:
                error = instance.getheader('location')[instance.getheader('location').find('error')+6:]
                return rc, 'LOGIN ERROR: '+ error
            # Look in location header of response to find the session we've been given.
            # This is needed to access data etc. later.    
            other = instance.getheader('location')[instance.getheader('location').find('session'):-1]
        else:
            other = instance.read()
        
        rc = True
        
        return rc, other
        
    @EntryExit
    def buildAndSendGet(self, getPath, noLine=False, redirect=False):
        """Method to build and send a get to CommPortal.
        
        getPath = Path (after /session/line) to get
        noLine = Optional parameter to indicate line shouldn't be included in
                 path
        redirect = Optional parameter to indicate redirect expected 
        
        Returns (rc, data) where
        rc = True if successfully and False otherwise
        data = Error info if unsuccessful and data returned if successful"""
        rc = False
        
        h1 = self._getConnection()
        headers = self._buildHttpHeaders()
        path = self._buildPathFrag(noLine) + getPath
        log.debug("Get path is: " + str(path))
        try:
            h1.request('GET', path, None, headers)
            instance = h1.getresponse()
            log.debug("Status code " + str(instance.status))
        except httplib.HTTPException, e:
            return rc, e
        except socket.timeout:
            return rc, 'Timed out.'
        except socket.error, e:
            return rc, 'Socket Error: '+ str(e)
        except Exception, e:
            return rc, 'Unknown error: ' + str(e)
        
        if not redirect and instance.status != 200:
            return rc, 'Error: %d %s' % (instance.status, instance.reason)
        elif redirect and instance.status != 302:
            return rc, 'Error: %d %s' % (instance.status, instance.reason)
        
        response = instance.read()
        rc = True
        
        return rc, response     
    
    @EntryExit
    def processJsonData(self, data):
        """Method to process JSON data returned by buildAndSendGet
        
        data = The data returned by buildAndSendGet
        
        Note that this method will only successfully process JSON data
        for one returned service indication.  Data from multiple indications
        will not be correct processed.
        
        Returns a dictionary containing the processed JSON data"""
        rc = None
        try:
            lines = data.splitlines()
            json = lines[2][:-1]
            processedData = simplejson.loads(json)
            rc = processedData
        except Exception, e:
            log.info("Failed to process JSON")
            log.info(traceback.format_exc())
        return rc

    # XXX Should make this function generic and take a parameter for which
    # notifications(s) the user is interested in.
    @EntryExit
    def registerForMessageNotifications(self, callback):
        """Method to register for notifications to changes to CommPortal
           messages (currently voicemails).
           
        callback = Callback to be called when there is a change to
                   messages.  You mustn't perform any blocking actions in
                   this callback method.
                      
        Returns a key to this registration which must be provided to
        unregister.

        The callback method supplied must take the following parameters:
        
        commportal = commportal object to allow the callback to call Library
                     methods
        event = Event which has occurred as returned by CommPortal"""
        if not self._loggedIn or not self.isAlive():
            log.warning("Not logged into CommPortal")
            raise CommPortal.NotLoggedInError
        thread = _MessagesNotificationThread(self, callback)
        self._workThreads.append(thread)
        thread.start()
        return thread
    
    @EntryExit
    def unregisterForMessageNotifications(self, key, wait=False):
        """Method to unregister for notifications to changes to CommPortal
           messages.
           
        key = Key returned by registration method.
        wait = Boolean indicating whether the user wants to wait for the
               registration to be removed."""
        thread = key
        thread.stop(wait)
        
    @EntryExit
    def downloadMessageCounts(self):
        """Method to download a the number of messages from CommPortal.
        
        Returns None is unsuccessful or the message count data."""
        if not self._loggedIn or not self.isAlive():
            log.warning("Not logged into CommPortal")
            raise CommPortal.NotLoggedInError
        rc, data =  self.buildAndSendGet('data?version=%s&callback=dummy&data=Meta_Subscriber_MetaSphere_VoicemailMessageCounts' % CommPortal.CP_VERSION_STRING)
        processedData = None
        if rc:
            processedData = self.processJsonData(data)
            log.debug(str(processedData))
        return processedData
        
    @EntryExit
    def downloadMessageList(self):
        """Method to download a list of messages from CommPortal.
        
        Returns None if unsuccessful or the message list data."""
        if not self._loggedIn or not self.isAlive():
            log.warning("Not logged into CommPortal")
            raise CommPortal.NotLoggedInError
        rc, data =  self.buildAndSendGet('data?version=%s&callback=dummy&data=Meta_Subscriber_MetaSphere_VoicemailMessages' % CommPortal.CP_VERSION_STRING)
        processedData = None
        if rc:
            processedData = self.processJsonData(data)
            log.debug(str(processedData))
        return processedData
    
    @EntryExit
    def downloadCallLists(self):
        """Method to download answered, dialed, missed and rejected calls from CommPortal.
        
        Note that currently this method only retrieves EAS Call Lists, not CFS Call Lists. 
        
        Returns None if unsuccessful or the call list data."""
        # XXX Need to retrieve CFS or EAS call lists as appropriate for subscriber.
        if not self._loggedIn or not self.isAlive():
            log.warning("Not logged into CommPortal")
            raise CommPortal.NotLoggedInError
        rc, data =  self.buildAndSendGet('data?version=%s&callback=dummy&data=Meta_Subscriber_MetaSphere_CallList' % CommPortal.CP_VERSION_STRING)
        processedData = None
        if rc:
            processedData = self.processJsonData(data)
            log.debug(str(processedData))
        return processedData
    
    @EntryExit
    def _buildMultipartFrags(self, frags):
        multipart = ""
        for frag in frags:
            multipart = "--%s\r\n" % CommPortal.MULTIPART_BOUNDARY
            multipart += 'Content-Disposition: form-data; name="%s"\r\n\r\n' % frag['name']
            multipart += '%s\r\n' % frag['value']
        multipart += '--%s' % CommPortal.MULTIPART_BOUNDARY
        return multipart
    
    @EntryExit
    def deleteMessage(self, message):
        """Method to delete a message from CommPortal.
        
        message = The message details downloaded from CommPortal"""
        jsonArgs = [{'_':message['Id']['_']}]
        postArgs = self._buildMultipartFrags(({'name':'Meta_Subscriber_MetaSphere_VoicemailMessagesToDelete',
                                               'value':simplejson.dumps(jsonArgs)},))
        rc, other = self.buildAndSendPost(postArgs,
                                 self._buildPathFrag() + 'data?version=%s' % CommPortal.CP_VERSION_STRING,
                                 postType = "MULTIPART_JSON") 
        if not rc:
            log.warning("Failed to delete message")
            log.info(str(other))
    
    @EntryExit
    def markMessageAsRead(self, message):
        """Method to mark a message as read.
        
        message = The message details downloaded from CommPortal"""
        jsonArgs = [{'_':message['Id']['_']}]
        postArgs = self._buildMultipartFrags(({'name':'Meta_Subscriber_MetaSphere_VoicemailMessagesToMarkAsRead',
                                               'value':simplejson.dumps(jsonArgs)},))
        rc, other = self.buildAndSendPost(postArgs,
                                 self._buildPathFrag() + 'data?version=%s' % CommPortal.CP_VERSION_STRING,
                                 postType = "MULTIPART_JSON") 
        if not rc:
            log.warning("Failed to mark message as read")
            log.info(str(other))
        
    @EntryExit
    def markMessageAsUnread(self, message):
        """Method to mark a message as read.
        
        message = The message details downloaded from CommPortal"""
        jsonArgs = [{'_':message['Id']['_']}]
        postArgs = self._buildMultipartFrags(({'name':'Meta_Subscriber_MetaSphere_VoicemailMessagesToMarkAsUnread',
                                               'value':simplejson.dumps(jsonArgs)},))
        rc, other = self.buildAndSendPost(postArgs,
                                 self._buildPathFrag() + 'data?version=%s' % CommPortal.CP_VERSION_STRING,
                                 postType = "MULTIPART_JSON") 
        if not rc:
            log.warning("Failed to delete message as unread")
            log.info(str(other))
        
    @EntryExit
    def downloadMessage(self, message, callback=None):
        """Method to download a message from CommPortal.
        
        message = The message details downloaded from CommPortal
        callback = Optional, defaults to None.  Provide a callback method if
                   you want the message to be downloaded asynchronously
                   (because you don't want your application to block while
                   the message downloads).  If set to None the message will 
                   be downloaded synchronously.
                   
        The callback method supplied must take the following parameters:
        
        commportal = commportal object to allow the callback to call Library
                     methods
        message = The message details as passed into this method
        messageData = The downloaded message or None if unsuccessful.
        
        Note that downloading a message automatically marks it as read.
        
        If callback is None, this method returns None if unsuccessful or the
        message file.  If callback is specified, returns a key to the
        request which must be provided if the request is cancelled."""
        if not self._loggedIn or not self.isAlive():
            log.warning("Not logged into CommPortal")
            raise CommPortal.NotLoggedInError
        if callback:
            thread = _MessageDownloadThread(self, message, callback)
            self._workThreads.append(thread)
            thread.start()
            return thread
        else:
            return self._downloadMessageNow(message)

    @EntryExit
    def cancelDownloadMessage(self, key, wait=0):
        """Method to cancel downloading a message.
           
        key = Key returned by downloadMessage method.
        wait = Boolean indicating whether the user wants to wait for the
               download to be cancelled."""
        thread = key
        thread.stop(wait)
    
    @EntryExit
    def _downloadMessageNow(self, message):
        messageData = None
        try:
            id = message['Id']['_']
            rc, data = self.buildAndSendGet('voicemail.wav?id=' + id)
            if rc:
                log.debug("Got message successfully")
                messageData = data
        except Exception, e:
            log.info("Failed to process JSON")
            log.info(traceback.format_exc())
        return messageData
        
    @EntryExit
    def downloadContacts(self):
        """Method to download a subscriber's contacts list.
        
        Returns None if unsuccessful or the contacts data."""
        if not self._loggedIn or not self.isAlive():
            log.warning("Not logged into CommPortal")
            raise CommPortal.NotLoggedInError
        rc, data =  self.buildAndSendGet('data?version=%s&callback=dummy&data=Meta_Subscriber_UC9000_Contacts' % CommPortal.CP_VERSION_STRING)
        processedData = None
        if rc:
            processedData = self.processJsonData(data)
            log.debug(str(processedData))
        return processedData

    @EntryExit
    def makeCall(self, destination, callback, source=None):
        """Method to make a click to dial call using this subscriber's
           account.
           
        destination = A string containing the number to be called.
        callback = Callback to be called when there is an update in the
                   status of the call being set up.  You mustn't perform
                   any blocking actions in this callback method.
        source = A String containing the number to call from.  This is the
                 number which will ring first.  This is an optional
                 parameter.  If omitted the subscriber's own number will
                 be used.
                      
        Returns a key to this call which must be provided to terminate it.
        None is returned if setting up the call is unsuccessful.
        
        The callback method supplied must take the following parameters:
        
        commportal = commportal object to allow the callback to call Library
                     methods
        events = Any call events
        errors = Any all errors
                      
        Note that once the call is fully set up, a ConnetionClearedEvent
        will be received after about 15s as CommPortal stops managing the
        call.  The call will continue until either party hang up."""
        # XXX Arguably not providing a source number should use the one the
        # user has configured in CommPortal.
        # XXX Should probably define the possible call statuses in the
        # documentation above.
        if not self._loggedIn or not self.isAlive():
            log.warning("Not logged into CommPortal")
            raise CommPortal.NotLoggedInError
        if source == None:
            source = self._dn
        thread = _MakeCallNotificationThread(self, destination, source, callback)
        rc, other = thread.makeCall()
        if rc:
            self._workThreads.append(thread)
            thread.start()
            rc = thread
        else:
            logging.info('Failed to make a call')
            logging.info(str(other))
            rc = None
        return rc
    
    @EntryExit
    def terminateCall(self, call):
        """Method to terminate a click to dial call.
        
        key = Key returned when the call was made."""
        if not self._loggedIn or not self.isAlive():
            log.warning("Not logged into CommPortal")
            raise CommPortal.NotLoggedInError
        call.terminateCall()
    
    @EntryExit    
    def run(self):
        self._logfn = CommPortal.HIGH_LEVEL_LOGGING
        counter = 0
        self._retryTimer = CommPortal.INITIAL_LOGIN_RETRY_TIMER
        self._reLogInNow = True
        while not self._stopNow:
            try:
                sleep = False
                if not (self._loggedIn):
                    log.debug("Not logged in")
                    if self._reLogInNow or (counter >= self._retryTimer):
                        self._reLogInNow = False
                        counter = 0
                        self._login()
                    else:
                        sleep = True
                else:
                    log.debug("Logged in")
                    if counter >= CommPortal.KEEPALIVE_TIMER:
                        counter = 0
                        self._keepAlive()
                    else:
                        sleep = True
                if sleep:
                    log.debug("Scheduler sleeping for " + str(CommPortal.SCHEDULER_INTERVAL) + " seconds")
                    time.sleep(CommPortal.SCHEDULER_INTERVAL)
                    counter += CommPortal.SCHEDULER_INTERVAL
            except Exception, e:
                log.error("Hit error in CommPortal run method")
                log.error(str(e))
                log.error(traceback.format_exc())
        if self._loggedIn:
            self._logfn = CommPortal.HIGH_LEVEL_LOGGING
            self._logout()
        self._stopNow = False
                    
    @EntryExit    
    def _loggedOut(self):
        self._loggedIn = False
        self._reLogInNow = True
        self._executeCallback(event=CommPortal.LOGGED_OUT)
        self._logfn = CommPortal.HIGH_LEVEL_LOGGING
        
    @EntryExit
    def _executeCallback(self, *args, **kwds):
        if self._callback:
            self.addCallback(self._callback, *args, **kwds)
            #self._callbackThread.addWork(self._callback, args, kwds)
            
    @EntryExit
    def addCallback(self, callback, *args, **kwds):
        self._callbackThread.addWork(callback, args, kwds)
        
    @EntryExit
    def _incrementRetryTimer(self):
        self._retryTimer *= 2
        if self._retryTimer > CommPortal.MAX_LOGIN_RETRY_TIMER:
            self._retryTimer = CommPortal.MAX_LOGIN_RETRY_TIMER
                    
    @EntryExit    
    def _login(self):
        try:
            rc, other = self._buildAndSendLogin()
            if rc:
                self._session = other
                self._loggedIn = True
                self._logfn("Successfully logged into CP for " + self._dn)
                self._executeCallback(event=CommPortal.LOGGED_IN)
            else:
                raise CommPortal.LogInFailedError
        except Exception, e:
            self._logfn("Hit error when attempting to log into CommPortal for " + self._dn)
            self._logfn(str(e))
            if type(e) == CommPortal.LogInFailedError:
                self._logfn(str(other))
            self._logfn(traceback.format_exc())
            self._incrementRetryTimer()
            self._logfn("Will try again in " + str(self._retryTimer) + " seconds")
            self._logfn("Subsequent failures may be logged at lower severity")
            self._logfn = CommPortal.LOW_LEVEL_LOGGING
            
    @EntryExit    
    def _keepAlive(self):
        try:
            rc, other = self._buildAndSendKeepAlive()
            if rc:
                self._logfn("Successfully keepalive CP session for " + self._dn)
                log.debug("Keepalive info returned: ")
                log.debug(other)
                self._logfn = CommPortal.LOW_LEVEL_LOGGING
            else:
                raise CommPortal.KeepAliveFailedError
        except Exception, e:
            self._logfn("Hit error when attempting to keepalive CommPortal session for " + self._dn)
            self._logfn(str(e))
            if type(e) == CommPortal.KeepAliveFailedError:
                self._logfn(str(other))
            self._logfn(traceback.format_exc())
            self._logfn("Subsequent failures may be logged at lower severity")
            self._logfn = CommPortal.LOW_LEVEL_LOGGING
            self._loggedOut()

    @EntryExit
    def _logout(self):
        try:
            rc, other = self._buildAndSendLogout()
            if rc:
                self._logfn("Successfully logged out")
            else:
                raise CommPortal.NotLoggedInError
        except Exception, e:
            self._logfn("Hit error when attempting to log out of CommPortal session for " + self._dn)
            self._logfn(str(e))
            if type(e) == CommPortal.NotLoggedInError:
                self._logfn(str(other))
            self._logfn(traceback.format_exc())
        self._loggedOut()
            
    @EntryExit    
    def _buildHttpHeaders(self, postType="ARGS"):
        if postType == "RAW_JSON":
            contentType = "application/json"
        elif postType == "MULTIPART_JSON":
            contentType = "multipart/form-data; boundary=%s" % CommPortal.MULTIPART_BOUNDARY
        else:
            contentType = "application/x-www-form-urlencoded"
        headers = {'Host':self._serverName,
                   'UserType':'bgAdmin',
                   'Content-Type': contentType,
                   'User-Agent':'Mozilla/5.0 (Windows; U; Windows NT 5.1;en-GB;\
                                 rv:1.9.0.12) Gecko/2009070611 Firefox/3.0.12',
                   'Keep-Alive':'300',
                   'Connection':'keep-alive'}
        return headers

    @EntryExit    
    def _getConnection(self):
        if self._https:
            h1 = httplib.HTTPSConnection(self._serverName)
        else:
            h1 = httplib.HTTPConnection(self._serverName)
        return h1
 
    @EntryExit
    def _buildPathFrag(self, noLine = False):
        if noLine:
            path = self._custPath + '/%s/' % (self._session)
        else:
            path = self._custPath + '/%s/line%s/' % (self._session, self._dn)
        return path
 
    @EntryExit    
    def _buildAndSendLogin(self):
        postArgs = {'DirectoryNumber': self._dn,
                     'Password' : self._password,
                     'errorRedirectTo' :'/login.html?redirectTo=%2Fline%2Fdefault.html',
                     'redirectTo':'/',
                     'version':CommPortal.CP_VERSION_STRING}
        return self.buildAndSendPost(postArgs, '/login', redirect=True)
    
    def _buildAndSendKeepAlive(self):
        return self.buildAndSendGet('data?version=%s&callback=dummy&data=Meta_Subscriber_MetaSphere_SubscriberSettings' % CommPortal.CP_VERSION_STRING)

    def _buildAndSendLogout(self):
        return self.buildAndSendGet('logout?redirectTo=/', noLine=True, redirect=True)

class _CallbackThread(threading.Thread):
    SCHEDULER_INTERVAL = 1
    
    @EntryExit
    def __init__(self, commportal):
        threading.Thread.__init__(self)
        self._workQueue = []
        self._stopNow = False
        self._commportal = commportal
        
    @EntryExit
    def addWork(self, fn, args, kwds):
        self._workQueue.append((fn, args, kwds))
        
    @EntryExit
    def stop(self):
        self._stopNow = True    
        
    @EntryExit
    def run(self):
        while not self._stopNow or len(self._workQueue):
            try:
                fn, args, kwds = self._workQueue.pop(0)
                log.debug("Work to do " + str(fn))
                try:
                    fn(self._commportal, *args, **kwds)
                except Exception, e:
                    log.info("Hit exception doing work")
                    log.info(str(e))
                    log.info(traceback.format_exc())
            except IndexError:
                log.debug("No work, sleeping")
                time.sleep(_CallbackThread.SCHEDULER_INTERVAL)
        self._stopNow = False
        
class _WorkThread(threading.Thread):
    
    @EntryExit
    def __init__(self, commportal, callback):
        threading.Thread.__init__(self)
        self._stopNow = False
        self._commportal = commportal
        self._callback = callback  
        
    @EntryExit
    def stop(self, wait=False):
        self._stopNow = True
        if wait:
            while self.isAlive():
                time.sleep(1)
        
    @EntryExit
    def _getName(self):
        raise CommPortal.NotImplementedError
        
    @EntryExit
    def _customExecuteRequest(self):
        raise CommPortal.NotImplementedError
    
    @EntryExit
    def _customProcessResponse(self, result):
        raise CommPortal.NotImplementedError
    
    @EntryExit
    def _processResponse(self, result):
        rc, data = result
        if rc:
            self._customProcessResponse(result)
        else:
            log.info("Error returned from " +
                    self._getName() +
                    " notification request")
            log.info(str(data))
    
    @EntryExit
    def _executeRequest(self):
        result = self._customExecuteRequest()
        return result
    
    @EntryExit
    def _addCallback(self, *args, **kwds):
        self._commportal.addCallback(self._callback, *args, **kwds)
    
    @EntryExit
    def run(self):
        # XXX Need to check logged in and deal with logouts etc.
        while not self._stopNow:
            try:
                result = self._executeRequest()
                if not self._stopNow:
                    # Handle window if stopped during wait but before
                    # callback then don't callback
                    self._processResponse(result)
            except Exception, e:
                log.info("Hit exception during Notification Thread "+ self._getName())
                log.info(str(e))
                log.info(traceback.format_exc())
        self._stopNow = False
        # Try and remove self from work threads.  May already have been
        # removed so do in a try.
        try:
            self._commportal._workThreads.remove(self)
        except:
            log.warning("Thread " + self._getName() +
                            " has already been removed from workThreads list")
                
class _MessageDownloadThread(_WorkThread):
    
    @EntryExit
    def __init__(self, commportal, message, callback):
        _WorkThread.__init__(self, commportal, callback)
        self._message = message

    @EntryExit
    def _getName(self):
        return "MessageDownload"   
    
    @EntryExit
    def _customExecuteRequest(self):
        rc = False
        result = self._commportal._downloadMessageNow(self._message)
        if result:
            rc = True
        return rc, result

    @EntryExit
    def _customProcessResponse(self, result):
        # Must stop work thread as we only want to download the message once.
        # Do this before adding the callback in case we hit an exception (and
        # therefore keep downloading the message forever!   
        self.stop(wait=False)     
        rc, data = result   
        self._addCallback(message=self._message, messageData=data)
                
class _MessagesNotificationThread(_WorkThread):
    
    @EntryExit
    def _getName(self):
        return "MessagesNotification"
    
    @EntryExit
    def _customExecuteRequest(self):
        rc, data = self._commportal.buildAndSendGet(
                  'events?version=' + CommPortal.CP_VERSION_STRING + 
                  '&events=Meta_Subscriber_MetaSphere_VoicemailMessageCounts')
        return rc, data
        
    @EntryExit
    def _customProcessResponse(self, result):
        rc, data = result
        assert(rc)
        log.debug("Data is " + str(data))
        processedData = simplejson.loads(data)
        events = processedData['events']
        if len(events) > 0 and events[0]['subscription'] == \
                          'Meta_Subscriber_MetaSphere_VoicemailMessageCounts':
            log.info("Have received an event")
            log.info(str(events))
            # Need to fire callback
            self._addCallback(event=events[0])
            
class _MakeCallNotificationThread(_WorkThread):
    
    @EntryExit
    def __init__(self, commportal, destination, source, callback):
        _WorkThread.__init__(self, commportal, callback)
        self._destination = destination
        self._source = source
        self._madeCall = False
        self._callId = None

    @EntryExit 
    def _getName(self):
        return "MakeCallNotification"
    
    @EntryExit
    def _customExecuteRequest(self):
        rc, data = self._commportal.buildAndSendGet(
                                'call%s/events?version=%s&events=Connection' % 
                                (self._callId, CommPortal.CP_VERSION_STRING))
        return rc, data
        
    @EntryExit
    def _customProcessResponse(self, result):
        rc, data = result
        assert(rc)
        log.debug("Data is " + str(data))
        processedData = simplejson.loads(data)
        error = False
        if len(processedData['errors']) > 0:
            log.info("Hit an error during call")
            log.info(str(processData['errors']))
            error = True
        if not error:
            for event in processedData['events']:
                eventType = event['eventType']
                if eventType in ['ConnectionClearedEvent',
                                 'FailedEvent',
                                 'PathClearedEvent'
                                 'PathReplacementEvent']:
                    log.info("Hit error event during call")
                    log.info(str(event))
                    error = True
        self._addCallback(events=processedData['events'],
                          errors=processedData['errors'])
        if error:
            self.stop()

    @EntryExit            
    def makeCall(self):
        postArgs = {"objectType":{"_":"Meta_CSTA_MakeCall"},
                    "callingDevice":{"_":self._source},
                    "calledDirectoryNumber":{"_":self._destination},
                    "autoOriginate":{"_":"doNotPrompt"}}
        rc, other = self._commportal.buildAndSendPost(postArgs,
                                 self._commportal._buildPathFrag() + 'action',
                                 postType = "RAW_JSON")
        if rc:
            try:
                processedData = simplejson.loads(other)
                self._callId = processedData['callingDevice']['callID']['_']
                self._madeCall = True
            except KeyError, IndexError:
                rc = False
                other = "Failed to parse data " + str(other)
        return rc, other
    
    @EntryExit
    def terminateCall(self):
        if not self._madeCall or not self._callId:
            log.info("Can't terminate call as don't have one up.")
            return 
        postArgs = {"objectType":{"_":"Meta_CSTA_ClearConnection"},
                    "connectionToBeCleared":{"callID":{"_":self._callId},
                    "deviceID":{"_":self._commportal._dn}}}
        rc, other = self._commportal.buildAndSendPost(postArgs,
              self._commportal.buildPathFrag + 'call%s/action' % self._callId,
              postType = "RAW_JSON")
        if not rc:
            # Should probably actually parse returned JSON but do we really
            # care if an error?
            log.info("Got error response when terminating call")
        self._callId = None
        self._madeCall = False    
        self.stop()
        
    
        
        
        