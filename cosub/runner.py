#!/usr/bin/env python

import sys, argparse, readline, os.path, pdb, csv, json, math, string, re, time
from pprint import pprint as pp

from datetime import datetime, timedelta
from pytimeparse.timeparse import timeparse

import logger, settings
import boto.mturk.connection as connection
import boto.mturk.question as question

from boto.mturk.qualification import LocaleRequirement, PercentAssignmentsApprovedRequirement, Qualifications
from boto.mturk.connection import MTurkRequestError

readline.parse_and_bind('set editing-mode emacs')

# HT http://stackoverflow.com/a/17303428/351392
class color:
   PURPLE = '\033[95m'
   CYAN = '\033[96m'
   DARKCYAN = '\033[36m'
   BLUE = '\033[94m'
   GREEN = '\033[92m'
   YELLOW = '\033[93m'
   RED = '\033[91m'
   BOLD = '\033[1m'
   UNDERLINE = '\033[4m'
   END = '\033[0m'

def bold(s):
  return color.BOLD + s + color.END

def underline(s):
  return color.UNDERLINE + s + color.END

def humane_timedelta(delta, precise=False, fromDate=None):
    # the timedelta structure does not have all units; bigger units are converted
    # into given smaller ones (hours -> seconds, minutes -> seconds, weeks > days, ...)
    # but we need all units:
    deltaMinutes      = delta.seconds // 60
    deltaHours        = delta.seconds // 3600
    deltaMinutes     -= deltaHours * 60
    deltaWeeks        = delta.days    // 7
    deltaSeconds      = delta.seconds - deltaMinutes * 60 - deltaHours * 3600
    deltaDays         = delta.days    - deltaWeeks * 7
    deltaMilliSeconds = delta.microseconds // 1000
    deltaMicroSeconds = delta.microseconds - deltaMilliSeconds * 1000

    valuesAndNames =[ (deltaWeeks  ,"week"  ), (deltaDays   ,"day"   ),
                      (deltaHours  ,"hour"  ), (deltaMinutes,"minute"),
                      (deltaSeconds,"second") ]
    if precise:
        valuesAndNames.append((deltaMilliSeconds, "millisecond"))
        valuesAndNames.append((deltaMicroSeconds, "microsecond"))

    text =""
    for value, name in valuesAndNames:
        if value > 0:
            text += len(text)   and ", " or ""
            text += "%d %s" % (value, name)
            text += (value > 1) and "s" or ""

    # replacing last occurrence of a comma by an 'and'
    if text.find(",") > 0:
        text = " and ".join(text.rsplit(", ",1))

    return text

def dict_str(d, level = 0):
  length_max = max(map(len, d.keys()))

  lines = []
  pad = 0

  any_dicts = any(map(lambda x: isinstance(x, dict),d.values()))

  for key in d:
    length = len(key)
    value = d[key]

    is_dict = isinstance(value, dict)

    lines.append("%(indent)s%(key)s:%(pad)s %(value)s" %
      {'indent': (level * "-> "),
      'key': key,
      'pad': (length_max - length)* ' ' + ('   ' if any_dicts else ''),
      'value': "\n" + dict_str(value, level + 1) if is_dict else str(value)
      })

  return "\n".join(lines)

def prints(*args):
  print("\n".join(args))

def without(array, element):
  new_array = array[:]
  if element in array:
    new_array.remove(element)
  return new_array

## mode settings (sandbox vs production)
if "-p" in sys.argv:
  mode = "production"
  in_sandbox = False
else:
  mode = "sandbox"
  in_sandbox = True
defaultNSs = 9
defaultDuration = "1 day"

## dialogue mode settings (silent vs verbose)
if "--silent" in sys.argv:
  dialogue_mode = "silent"
else:
  dialogue_mode = "verbose"
print dialogue_mode

HOST = {
  'sandbox': 'mechanicalturk.sandbox.amazonaws.com',
  'production': 'mechanicalturk.amazonaws.com'
}[mode]

HOST_requester = "http://" + ("requestersandbox" if in_sandbox else "requester") + ".mturk.com"
HOST_worker = "http://" + ("workersandbox" if in_sandbox else "www") + ".mturk.com"

argv = sys.argv[1:]

def usage():
  prints(
    "Usage: cosub [-p] ACTION",
    "",
    "Flags:",
    "   -p: production mode (if this isn't set, cosub will run in the sandbox)",
    "   --silent: run silently with default #Ss (" + str(defaultNSs) + ") and default experiment duration (" + defaultDuration + ")",
    "",
    "Actions:",
    "   create               (create a HIT using the parameters stored in settings.json)",
    "   update (TODO)        (update the HIT using the parameters stored in settings.json)",
    "   add <N> assignments",
    "   add <N> <time>       (time can be hours, minutes, or seconds)",
    "   expire",
    "   status",
    "   download             (download results to production-results/ or sandbox-results/)",
    "")
  sys.exit()

## if no args, bail
## TODO: add --comment flag
if len(argv) == 0:
  usage()

if not os.path.isfile("auth.json"):
  sys.exit("Couldn't find credentials file auth.json")

action = " ".join(
  without(
    without(argv, "-p"), "--silent"
  )
).lower()

# read authentication data
auth_data = json.load(open("auth.json", "r"))
ACCESS_ID = auth_data["access_id"]
SECRET_KEY = auth_data["secret_key"]

settings_filename = "settings.json"

logger.setup()

with open(settings_filename, "r") as f:
  lines = f.readlines()
  # remove comments in json
  lines = map(lambda line: re.sub("/\*.*\*/", "", line), lines)
  settings_file_contents = "".join(lines)
settings_raw = json.loads(settings_file_contents)
#settings_log_text = None
# for line in log:
#   if line['Activity'] in ['Create', 'Update']:
#     settings_log_text = line['Data']
# settings_log_raw = json.loads(settings_log_text) if settings_log_text else settings_raw
# settings_in_log = settings.parse(settings_log_raw)
settings_in_file = settings.parse(settings_raw)
# settings_modified = (action is not "show status" and settings_in_log is not settings_in_file)
# TODO: bail if settings modified
settings = settings_in_file

## load hit metadata if it exists
hit_modes = dict()
hit = None
if os.path.isfile("hit_modes.json"):
  hit_modes = json.load(open("hit_modes.json", "r"))
  if mode in hit_modes:
    hit = hit_modes[mode]

## connect to amazon
mtc = connection.MTurkConnection(aws_access_key_id=ACCESS_ID,
                                 aws_secret_access_key=SECRET_KEY,
                                 host=HOST)

def create_hit(settings):
  global hit
  ## make sure there isn't already a hit
  if (hit is not None):
    sys.exit("Error: it looks like you already created the hit in %s mode (HIT ID stored in hit_modes.json)" % mode)

  hit_quals = Qualifications()
  settings_quals = settings["qualifications"]
  ## TODO: master worker, custom quals, utility for creating qualifications?
  if (settings_quals):
    if settings_quals["location"]:
      hit_quals.add(LocaleRequirement("EqualTo", settings_quals["location"]))

    if settings_quals["approval_percentage"]:
      hit_quals.add(PercentAssignmentsApprovedRequirement("GreaterThanOrEqualTo",
                                                          settings_quals["approval_percentage"]))

  prints(
    "Your settings are:",
    "",
    dict_str(settings_raw)
    )

  ##if "http:" in settings["url"]:
   ## sys.exit("Error: inside settings.json, your url is set to use 'http:'. It needs to use 'https:'")
    ## todo: some static analysis

  if dialogue_mode=="verbose":
    prints(
      "",
      "Are these settings okay? (yes/no)")
    confirm_settings = raw_input("> ")
  else:
    confirm_settings = "yes"

  if "n" in confirm_settings:
    sys.exit()

  if dialogue_mode=="verbose":
    prints(
      "",
      "How many assignments do you want to start with?",
      "(you can always add more later using cosub add)")

    max_assignments = None

    while max_assignments is None:
      try:
        max_assignments = int(raw_input("> "))
      except ValueError:
        prints("Couldn't understand answer. Try entering a positive integer (e.g., 20)")
  else:
    prints("You will start with " + str(defaultNSs) + " assignments.")
    max_assignments = defaultNSs

  if mode == "production":
    reward = settings["reward"]
    cost = max_assignments * float(reward)
    fee = 0.4 if max_assignments > 9 else 0.2
    fee_str = "40%" if fee == 0.4 else "20%"
    prints(
      "The cost will be $%.2f -- %s assignments * $%.2f/assignment + %s fee" % (cost, max_assignments, reward, fee_str)
    )
    if dialogue_mode=="verbose":
      prints("Is this okay? (yes/no)")
      confirm_cost = raw_input("> ")
      if "n" in confirm_cost:
        sys.exit()
  else:
    print("(This won't cost anything because you're in sandbox mode)")

  ## TODO: implement bounds checking for assignments

  if dialogue_mode=="verbose":
    prints(
      "",
      "How long do you want to collect data for?",
      "You can give an answer in seconds, minutes, hours, days, or weeks.",
      "(and you can always add more time using cosub add)")

    lifetime_seconds = None

    while lifetime_seconds is None:
      lifetime = raw_input("> ")
      lifetime_seconds = timeparse(lifetime)
      if not lifetime_seconds:
        prints("Couldn't understand answer; try an answer in one of these formats:",
          "  2 weeks",
          "  3 days",
          "  12 hours",
          "  30 minutes")
  else:
      prints("You will collect data for " + defaultDuration + ".")
      lifetime_seconds = timeparse(defaultDuration)

  ## TODO: implement bounds checking for time (30 seconds - 1 year)

  prints("","Creating HIT...","")

  request_settings = dict(
    title           = settings["title"],
    description     = settings["description"],
    keywords        = settings["keywords"],
    question        = question.HTMLQuestion(settings["url"], settings["frame_height"]),
    max_assignments = max_assignments,
    reward          = settings["reward"],
    approval_delay  = timedelta(seconds = settings["auto_approval_delay"]),
    duration        = timedelta(seconds = settings["assignment_duration"]),
    lifetime        = timedelta(seconds = lifetime_seconds),
    qualifications  = hit_quals
  )

  try:
    create_result = mtc.create_hit(**request_settings)[0]
  except MTurkRequestError as e:
    print("Error\n")
    pp(e.__dict__)
    sys.exit(1)

  hit = {
    "id": create_result.HITId,
    # hit_group_id = hit.HITGroupId,
    "type_id": create_result.HITTypeId
  }

  hit_modes[mode] = hit

  print("Successfully created HIT")
  ## write hit and HITTypeId into even-odd.json
  with open("hit_modes.json", 'w') as new_settings_file:
    json.dump(hit_modes, new_settings_file, indent=4, separators=(',', ': '))
    print("Wrote HIT ID and HIT Type ID to hit_modes.json")

  prints(
    "- The number of initial assignments is set to %s" % request_settings["max_assignments"],
    "- The initial HIT lifetime is set to %s" % humane_timedelta(request_settings["lifetime"]))

  prints(
    "",
    "Manage HIT: ",
    HOST_requester + "/mturk/manageHIT?HITId=" + hit["id"])

  prints(
    "",
    "View HIT: ",
    HOST_worker + "/mturk/preview?groupId=" + hit["type_id"],
    "")

  logger.write({'Action': 'Create', 'Data': settings_raw })

def update_hit(settings):
  global hit
  hit_quals = Qualifications()
  old_hit_type_id = hit["type_id"]
  settings_quals = settings["qualifications"]
  ## TODO: master worker, custom quals, utility for creating qualifications?
  if (settings_quals):
    if settings_quals["location"]:
      hit_quals.add(LocaleRequirement("EqualTo", settings_quals["location"]))

    if settings_quals["approval_percentage"]:
      hit_quals.add(PercentAssignmentsApprovedRequirement("GreaterThanOrEqualTo",
                                                          settings_quals["approval_percentage"]))

  request_settings = dict(
    title           = settings["title"],
    description     = settings["description"],
    keywords        = settings["keywords"],
    reward          = settings["reward"],
    approval_delay  = timedelta(seconds = settings["auto_approval_delay"]),
    duration        = timedelta(seconds = settings["assignment_duration"]),
    qual_req        = hit_quals
  )

  try:
    new_hit_type_id = mtc.register_hit_type(**request_settings)[0].HITTypeId

    if new_hit_type_id == old_hit_type_id:
      prints("Settings haven't changed; not updating")
      sys.exit(1)

    change_existing_hit_type_result = mtc.change_hit_type_of_hit(hit["id"], new_hit_type_id)
  except MTurkRequestError as e:
    print("Error\n")
    pp(e.__dict__)

  hit_modes[mode]["type_id"] = new_hit_type_id

  ## write new_hit_type_id to hit_modes.json
  with open("hit_modes.json", 'w') as f:
    json.dump(hit_modes, f, indent=4, separators=(',', ': '))

  prints("Updated from type:",
    old_hit_type_id,
    "to type:",
    new_hit_type_id)

def get_results(host, mode, hit_id):
  results_dir = "%s-results" % mode

  if not os.path.exists(results_dir):
    os.makedirs(results_dir)

  page_size = 50.0

  ## based on number of files in results_dir, find the number of pages we've already downloaded
  downloaded_assignments = map(lambda _: _.replace(".json",""),
                               filter(lambda _: _.find(".json") > - 1,
                                      os.listdir(results_dir)))
  num_downloaded_assignments = len(downloaded_assignments)
  print("Currently have " + str(num_downloaded_assignments) + " results")

  if num_downloaded_assignments % int(page_size) == 0:
    num_downloaded_pages = (num_downloaded_assignments / int(page_size)) + 1
  else:
    num_downloaded_pages = int(math.ceil(num_downloaded_assignments / page_size))

  ## submit a dummy request for page_size = 1 so that we can get the total number of assignments
  num_total_assignments = int( mtc.get_assignments(hit_id, page_size = 1).TotalNumResults )
  num_total_pages = int(math.ceil(num_total_assignments / page_size))
  print("Mturk has " + str(num_total_assignments) + " results")

  if num_downloaded_assignments == num_total_assignments:
    sys.exit("Done")

  assignments_to_write = []

  for i in range(num_downloaded_pages, num_total_pages + 1):
    print("Downloading page " + str(i) + " of results")
    assignments_to_write += mtc.get_assignments(hit_id, page_size = int(page_size), page_number = i)

  for a in assignments_to_write:
    aId = a.AssignmentId

    ## if we've downloaded this one before, don't write to disk
    if aId in downloaded_assignments:
      print("Skipped " + aId)
      continue

    ## otherwise, write to disk
    data = a.__dict__

    answers_dict = dict()

    for answer in a.answers:
      for question_form_answer in answer:
        field_name = question_form_answer.qid
        field_value = question_form_answer.fields[0]

        try:
          answers_dict[field_name] = json.loads( field_value  )
        except:
          if (field_value == 'undefined'):
            answers_dict[field_name] = None

    ## overwrite the array of QuestionFormAnswer objects
    data["answers"] = answers_dict

    with open(results_dir + "/" + aId + ".json","w") as f:
      jsonData = json.dumps(data, indent=4, separators=(',', ': '))
      f.write(jsonData)

    print("Wrote   " + aId)
  print("Done")

def add_time(hit, n):
  res = mtc.extend_hit(hit_id = hit["id"], expiration_increment = n)
  logger.write({'Action': 'Add', 'Data': '%s' % humane_timedelta(timedelta(seconds = n))})

def add_assignments(hit, n):
  res = mtc.extend_hit(hit_id = hit["id"], assignments_increment = n)
  logger.write({'Action': 'Add', 'Data': '%s assignments' % n})

def expire_hit(hit):
  res = mtc.expire_hit(hit_id = hit["id"])
  logger.write({'Action': 'Expire', 'Data': ''})
  print("Done")

def show_status(hit):
  prints("",
    "Basic",
    "=====================",
    dict_str({
      "Mode": mode,
      "HIT ID": hit["id"],
      "HIT Type ID": hit["type_id"]}),
    "")

  prints(
    "Settings",
    "=====================",
    dict_str(settings_raw),
    "")

  prints(
    "Links",
    "=====================",
    dict_str({
      "Manage": "\n%(host)s/mturk/manageHIT?HITId=%(id)s\n" % {"host": HOST_requester, "id": hit["id"]},
      "View": "\n%(host)s/mturk/preview?groupId=%(id)s" % {"host": HOST_worker, "id": hit["type_id"]}
    }),
    "")

  hit_remote = mtc.get_hit(hit["id"], response_groups = ["Request","Minimal","HITDetail","HITQuestion","HITAssignmentSummary"])[0]

  expiration = datetime.strptime(hit_remote.Expiration, '%Y-%m-%dT%H:%M:%SZ')
  now = datetime.utcnow()

  time_left = expiration - now

  prints(
    "Collection",
    "=====================",
    dict_str({
    "Time remaining": humane_timedelta(time_left) if time_left.total_seconds() > 0 else "Expired",
    "Assignments": {
      "Remaining": hit_remote.NumberOfAssignmentsAvailable,
      "Completed": hit_remote.NumberOfAssignmentsCompleted,
      "Pending": hit_remote.NumberOfAssignmentsPending
    }}))

  sys.exit()

def go():
  if not ("create" in action) and hit is None:
    sys.exit("You haven't created the hit on Turk yet (mode: %s)" % mode)

  print(bold(underline("%s mode" % string.capwords(mode))))

  if action == "create":
    create_hit(settings)
  elif action == "update":
    update_hit(settings)
  elif action == "download":
    get_results(HOST, mode, hit["id"])
  ## add time, assignments, or both
  elif re.match("^add ", action):
    action_ = re.sub("add ","", action)
    num_assignments = 0
    td = None
    # extract assignments
    assignments_search = re.search("([0-9]+) *assignments", action_)
    if (assignments_search):
      num_assignments = int(assignments_search.group(1))
      print("Adding %d assignments" % num_assignments)
      action_ = re.sub(assignments_search.group(0), "", action_)
      action_ = re.sub("and", "", action_)
      add_assignments(hit, num_assignments)
      print("-> Done")

    # time parse the rest
    seconds = timeparse(action_)

    if (seconds is not None):
      print("Adding %s" % humane_timedelta(timedelta(seconds = seconds)))
      add_time(hit, seconds)
      print("-> Done" )
  elif action == "status":
    show_status(hit)
  elif action == "expire":
    expire_hit(hit)
  elif action == "history":
    sys.exit("Not yet implemented")
  elif action == "manage":
    sys.exit("Not yet implemented")
  elif action == "view":
    sys.exit("Not yet implemented")
  else:
    usage()

if __name__ == "__main__":
    go()
