#!/usr/bin/python
# Clem 2019-01-25
# - Display armed/disarmed status of Arlo cameras on LCD
# - Arm/disarm on correct PIN input
# - Take picture and upload to website on wrong PIN, displaying message on LCD
# - Send IFTTT notifications on arm / disarm / wrong PIN (last one includes pic taken from camera)

import subprocess
import time
import threading
import Queue
import LCD_1in8
import LCD_Config
import Image
import ImageDraw
import ImageFont
import sys
import os
import select
import logging
import logging.handlers
import ftplib
import datetime
import argparse
import json
import requests
import traceback
import evdev
import signal
from enum import Enum
from Arlo import Arlo
from picamera import PiCamera
from contextlib import closing

class PinEntered(Enum):
    Unknown = 0
    Right = 1
    Wrong = 2
    Quit = 3
    Refresh = 4

class IFTTTNotification(Enum):
    #These values must correspond to actual IFTTT Webhooks event names created in your IFTT account
    Armed = "armed"
    Disarmed = "disarmed"
    WrongPIN = "wrongPIN"

class ArloManager(object):
    user = "xxxx@xxxxx.xxx" #recommend creating a dedicated Arlo user name so as to not get logged off other devices
    passwd = "xxxxxxxxx" #yey security! Recommend using an encrypted filesystem
    arlo = None
    basestation = None
    camera = None

    def connect(self):
        self.arlo = Arlo(self.user, self.passwd)
        self.basestation = self.arlo.GetDevices('basestation')[0] #assuming there is only one base station

    def arm(self):
        self.arlo.Arm(self.basestation)

    def disarm(self):
        self.arlo.Disarm(self.basestation)

    def getArmed(self):
        status = self.arlo.GetModes(self.basestation)
        if (status == None):
            return None #probably token expired
        if (status['properties']['active'] == u'mode0'):
            return False
        else:
            return True

def getCommandOutput(cmd):
    ps = subprocess.Popen(cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    output = ps.communicate()[0].replace('\n','')
    return output

def drawImageOnLCD(lcd, arlo_armed, wrongPIN = False):
    image = Image.new("RGB", (lcd.LCD_Dis_Column, lcd.LCD_Dis_Page), "BLACK")
    draw = ImageDraw.Draw(image)
    font = ImageFont.truetype('zektonrg.ttf', 24) #need that font in the same dir as this script

    if (wrongPIN):
        draw.text((5, 40), "!! Wrong PIN !!", fill="RED", font=font)
        draw.text((30, 90), "Picture uploaded", fill="WHITE")
    else:
        if (arlo_armed == None):
            draw.text((45,45), "Wait...", fill="WHITE", font=font)
        elif (arlo_armed):
            draw.text((40,45), "Armed", fill="RED", font=font)
        else:
            draw.text((25,45), "Disarmed", fill="GREEN", font=font)
    lcd.LCD_ShowImage(image, 0, 0)

#Take a picture, upload to specified FTP host, return image URL
def snapAndUpload():
    ftp_user = "xxxxx"
    ftp_pass = "xxxxxx"
    ftp_host = "ftp.xxxxxx.xxxx"
    ftp_dir = "/www/xxxxxxx"
    filename = "snap" + '{:%Y-%m-%d--%H-%M-%S}'.format(datetime.datetime.now()) + ".jpg"

    camera = PiCamera()
    camera.capture(filename)
    camera.close()
    Image.open(filename).rotate(-90, expand=True).save(filename) #my camera is rotated
    with closing(ftplib.FTP(ftp_host)) as ftp:
        ftp.login(ftp_user, ftp_pass)
        ftp.cwd(ftp_dir)
        with open(filename, 'rb') as file:
            res = ftp.storbinary("STOR " + filename, file, 1024)
            if not res.startswith('226'):
                raise Exception("FTP upload failed")
    os.remove(filename)

    return "http://www.xxxx.xxx" + ftp_dir.replace('/www', '') + '/' + filename

def sendIFTTTNotification(notif, picture=None, debug=False):
    key = "xxxxxxxxxxxx" #Webhooks IFTTT applet key
    if(notif == IFTTTNotification.WrongPIN):
        body = json.dumps({'value1': picture})
    else:
        body = ''
    url = 'https://maker.ifttt.com/trigger/' + notif.value +'/with/key/' + key

    if(debug):
        print("Sending IFTTT request - url= " + url + " - body= " + body)

    requests.post(url,
        headers={'Content-Type': 'application/json'},
        data=body)

#Get and update status, update the LCD
def monitorAndUpdate(pinEntered, logger, debug, q):
    lcd = LCD_1in8.LCD()
    lcd.LCD_Init(LCD_1in8.SCAN_DIR_DFT)

    arlo = ArloManager()
    arlo.connect()
    arlo_armed = False
    old_arlo_armed = False

    drawImageOnLCD(lcd, arlo_armed)

    while(True):
        refresh = False
        wrongPIN = False
        if (debug):
            print("________")
            print("time: " + str(datetime.datetime.now()))

        try:
            arlo_armed = arlo.getArmed()
            if (debug):
                print "arlo armed: " + str(arlo_armed)
            if (arlo_armed == None):
                logger.error('Connection error')
                arlo_armed = old_arlo_armed #don't store "None" for next loop's comparison
                arlo.connect()
                logger.info('Refreshed Arlo connection token')
        except Exception, e:
            logger.error('Could not get Arlo status: ' + str(e))
            arlo_armed = old_arlo_armed
            if(debug):
                logger.error(traceback.format_exc())

        #write status to file system for other scripts to read
        with open('arlo_armed', 'w') as file:
            file.write(str(arlo_armed))

        if (arlo_armed != old_arlo_armed):
            refresh = True
            if (arlo_armed):
                sendIFTTTNotification(IFTTTNotification.Armed, debug=debug)
            else:
                sendIFTTTNotification(IFTTTNotification.Disarmed, debug=debug)

        if debug:
            print "refresh: " + str(refresh)
            print "ram=" + getCommandOutput("free -h | grep Mem | gawk '{print $7}'")
            print getCommandOutput("vcgencmd measure_temp")

        try: #see if a PIN was entered and change armed state accordingly
            pinEntered = q.get(timeout=2)

            if(pinEntered == PinEntered.Right): #flip arlo arm/disarm status
                logger.info("Correct PIN entered")
                drawImageOnLCD(lcd, None) #this will display "Wait..." until next loop
                if(arlo_armed):
                    arlo.disarm()
                else:
                    arlo.arm()

            elif(pinEntered == PinEntered.Wrong): #take picture, upload and display message, send IFTTT notification
                logger.error("Wrong PIN entered")
                try:
                    snapURL = snapAndUpload()
                    sendIFTTTNotification(IFTTTNotification.WrongPIN, snapURL, debug=debug)
                except Exception, e:
                    logger.error('Could not take and upload snap: ' + str(e))
                wrongPIN = True
                refresh = True

            elif(pinEntered == PinEntered.Refresh):
                logger.info('Force refresh')
                refresh = True

            elif(pinEntered == PinEntered.Quit):
                logger.info("Quit")
                sys.exit(0) #stop this thread

        except Queue.Empty: #no PIN entered within the timeout window, just keep waiting
            pass
        except Exception, e:
            logger.error('Could not arm/disarm: ' + str(e))

        if(refresh):
            old_arlo_armed = arlo_armed
            drawImageOnLCD(lcd, arlo_armed, wrongPIN)

        time.sleep(2) #avoid high CPU usage

#Listen for PIN keyboard input and send result to the monitoring thread
def listenForPin(pinEntered, q):
    typed = ''
    device = evdev.InputDevice('/dev/input/event0') #keypad is the only input device plugged in
    device.grab() #avoid causing key presses to register in the console's login prompt
    for event in device.read_loop():
        if (event.type == evdev.ecodes.EV_KEY):
            data = evdev.categorize(event)
            if (data.keystate == 1): #key down
                key = data.keycode.replace('KEY_KP', '')
                if (key == 'ENTER'):
                    if (typed == '1234'): #your PIN here
                        pinEntered = PinEntered.Right
                    elif (typed == 'SLASHSLASH'):
                        pinEntered = PinEntered.Quit
                    elif (typed == "DOT"):
                        pinEntered = PinEntered.Refresh
                    else:
                        pinEntered = PinEntered.Wrong
                    q.put(pinEntered)
                    if (pinEntered == PinEntered.Quit):
                        sys.exit(0) #stop this thread
                    typed = ''
                else:
                    typed = typed + key

if (__name__ == "__main__"):
    debug = False
    kill = False
    pidfile = '/tmp/keypad_pid'
    
    argsparser = argparse.ArgumentParser(description='PIN based Arlo armer/disarmer')
    argsparser.add_argument('-d', '--debug', help='output debugging stuff', action='store_true')
    argsparser.add_argument('-k', '--kill', help='kill running instance', action='store_true')
    args = argsparser.parse_args()
    if (args.debug):
        debug = True
    if (args.kill):
        kill = True

    logger = logging.getLogger('numpad arming')
    logger.setLevel(logging.DEBUG)
    sysloghandler = logging.handlers.SysLogHandler(address = '/dev/log')
    formatter = logging.Formatter('numpad arming: %(message)s')
    sysloghandler.setFormatter(formatter)
    logger.addHandler(sysloghandler)
    consolehandler = logging.StreamHandler()
    logger.addHandler(consolehandler)

    #write pid in a file so we know what to kill if invoked with -k
    if (not kill): #don't write this process' PID if invoked to kill an already instance
        with open(pidfile, 'w') as file:
            pid = str(os.getpid())
            file.write(pid)
            if (debug):
                logger.debug('PID: ' + pid)


    if (kill):
        logger.info('Killing running instance')
        try:
            with open(pidfile, 'r') as file:
                pid = file.read()
                if (debug):
                    logger.debug('PID to kill: ' + pid)
                os.kill(int(pid), signal.SIGKILL)
                os.remove(pidfile)
        except Exception,e :
            logger.error('Could not find a running instance')
        sys.exit(0)


    pinEntered = PinEntered.Unknown
    q = Queue.Queue()
    inputThread = threading.Thread(target=listenForPin, args=(pinEntered,q))
    monitorThread = threading.Thread(target=monitorAndUpdate, args=(pinEntered,logger,debug,q))
    inputThread.start()
    monitorThread.start()
    logger.info("Started")
    print("Input PIN then <Enter> to arm/disarm; '/' then <Enter> to quit; '.' then <Enter> to force refresh")
