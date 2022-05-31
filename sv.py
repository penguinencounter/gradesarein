import hashlib
import json
import logging
import os.path
import string
import typing
from collections import OrderedDict
from datetime import datetime

import requests
import zeep
from studentvue import StudentVue

TIMEOUT_TRANSFER = zeep.Transport(timeout=5)


class Assignment:
    def __init__(self, assignment_id: int, name: str, score: int):
        self.assignment_id = assignment_id
        self.name = name
        self.score = score

    def pack(self):
        return {
            'assignment_id': self.assignment_id,
            'name': self.name,
            'score': self.score
        }

    @classmethod
    def unpack(cls, data: dict):
        return cls(data['assignment_id'], data['name'], data['score'])


class CourseTracker:
    def __init__(self, course_name: str):
        self.name = course_name
        self.assignments = []

    def add_assignments_from_sv(self, assignments: list):
        for a in assignments:
            b = Assignment(a['@GradebookID'], a['@Measure'], a['@Score'])
            self.assignments.append(b)

    def add_assignments_from_pack(self, assignments: list):
        for a in assignments:
            self.assignments.append(Assignment.unpack(a))

    def pack(self):
        return {
            'name': self.name,
            'assignments': [a.pack() for a in self.assignments]
        }

    @classmethod
    def unpack(cls, data: dict):
        c = cls(data['name'])
        c.add_assignments_from_pack(data['assignments'])
        return c


def check_auth():
    AUTH_TEMPLATE = \
        "username: [put your username here]\npassword: [put your password here]\ndomain: [put your domain here (the " \
        "URL you use to login to studentvue, like sis.powayusd.com)]\nhook: [put the URL to your webhook here]"
    if not os.path.exists('secret.conf'):
        with open('secret.conf', 'w') as f:
            f.write(AUTH_TEMPLATE)
        logging.error('Please edit the secret.conf file with your username and password for StudentVue. (a template '
                      'has been created for you)')
        return False
    with open('secret.conf', 'r') as f:
        auth_data = f.read()
    auth_data = auth_data.split('\n')
    auth_data = {k.strip(): v.strip() for k, v in (line.split(':') for line in auth_data if line.strip() != '')}
    for v in auth_data.values():
        if '[' in v or ']' in v:
            logging.error('Please edit the secret.conf file with your username and password for StudentVue.')
            return False
    if 'username' not in auth_data.keys() \
            or 'password' not in auth_data.keys() \
            or 'domain' not in auth_data.keys() \
            or 'hook' not in auth_data.keys():
        logging.error('Some data is missing from the secret.conf file. Edit it, or delete it and run this script '
                      'again to generate a template.')
        return False
    return auth_data


def make_safe_filename(input_filename):
    try:
        safechars = string.ascii_letters + string.digits + " -_."
        return filter(lambda c: c in safechars, input_filename)
    except:
        return ""


def track_rp(sv: StudentVue, rp: dict):
    logging.info('Beginning tracking of grade period {} (id {})'.format(rp['@GradePeriod'], rp['@Index']))
    # get gradebook period
    gb = sv.get_gradebook(rp['@Index'])['Gradebook']['Courses']['Course']

    def process_course(course: OrderedDict):
        result_data = {
            "new": [],
            "updated": [],
            "removed": [],
            "score": {
                "letter": "Not Calculated",
                "percent": -1
            }
        }

        course_name = course['@Title']
        course_gen_id = hashlib.sha256(course_name.encode('utf-8')).hexdigest()
        mark = course['Marks']['Mark']
        result_data["score"]["letter"] = mark['@CalculatedScoreString']
        result_data["score"]["percent"] = mark['@CalculatedScoreRaw']
        assignments = mark['Assignments']
        # pprint(assignments)
        if 'Assignment' in assignments.keys():
            assignments = assignments['Assignment']
            ct = CourseTracker(course_name)
            ct.add_assignments_from_sv(assignments)
            previous = None
            if os.path.exists(f'sv_tracked_{course_gen_id}.json'):
                with open(f'sv_tracked_{course_gen_id}.json', 'r') as f:
                    tracked = json.load(f)
                    previous = CourseTracker.unpack(tracked)

            with open(f'sv_tracked_{course_gen_id}.json', 'w') as f:
                json.dump(ct.pack(), f, indent=2)

            def isnew(assignment_to_check, assignments_to_compare_against):
                for a in assignments_to_compare_against:
                    if assignment_to_check.assignment_id == a.assignment_id:
                        return False
                return True

            def find(assignment_to_find, source):
                for a in source:
                    if a.assignment_id == assignment_to_find.assignment_id:
                        return a
                return None

            if previous is not None:
                for assignment in ct.assignments:
                    if isnew(assignment, previous.assignments):
                        result_data['new'].append(assignment)
                    else:
                        previous_assignment = find(assignment, previous.assignments)
                        if assignment.score != previous_assignment.score:
                            result_data['updated'].append((previous_assignment, assignment))
                for assignment in previous.assignments:
                    if isnew(assignment, ct.assignments):
                        result_data['removed'].append(assignment)
            else:
                result_data['new'] = ct.assignments
        return result_data

    results = {}

    if type(gb) == list:
        for course in gb:
            results[course['@Title']] = process_course(course)
    else:
        results[gb['@Title']] = process_course(gb)
    return results


def niceify(data:
typing.Dict[
    str, typing.Dict[
        str,
        typing.Union[
            typing.List[Assignment],
            typing.List[typing.Tuple[Assignment, Assignment]],
            typing.Dict[str, typing.Union[str, float]]
        ]
    ]
]
            ):
    output = ""
    for course, result in data.items():
        if len([b for k, v in result.items() if k in ['new', 'updated', 'removed'] for b in v]) == 0:
            continue
        output += f'{course}:\n'
        for key, value in result.items():
            if len(value) == 0 or key == 'score':
                continue
            output += f'  {key}:\n'
            for assignment in value:
                if type(assignment) == tuple:
                    assignment: typing.Tuple[Assignment, Assignment]
                    output += f'    {assignment[0].assignment_id}: {assignment[0].score} -> {assignment[1].score}\n'
                else:
                    assignment: Assignment
                    output += f'    {assignment.assignment_id}: {assignment.score}\n'
        output += f'New grade is {result["score"]["letter"]} ({result["score"]["percent"]}%)\n'
    return output


def main():
    logging.basicConfig(format='[%(asctime)s] [%(levelname)s] %(message)s',
                        level=logging.DEBUG,
                        datefmt='%H:%M:%S')
    auth_data = check_auth()
    if not auth_data:
        return
    logging.info('Started successfully! Logging in...')
    sv = StudentVue(auth_data['username'], auth_data['password'], auth_data['domain'], zeep_transport=TIMEOUT_TRANSFER)
    logging.info('Logged in successfully!')
    rps = sv.get_gradebook()['Gradebook']['ReportingPeriods']['ReportPeriod']
    logging.info(f'Guessing correct grade period, of {len(rps)}...')
    rps_to_track = []
    for rp in rps:
        rp: OrderedDict
        s = rp['@StartDate']
        e = rp['@EndDate']
        so = datetime.strptime(s, '%m/%d/%Y')
        eo = datetime.strptime(e, '%m/%d/%Y')
        if so <= datetime.now() <= eo:
            logging.info('Found active grade period: {} (id {})'.format(rp['@GradePeriod'], rp['@Index']))
            rps_to_track.append({k: v for k, v in rp.items()})
    logging.info(f'Tracking {len(rps_to_track)} grade periods...')
    outputs = ""
    for rp in rps_to_track:
        outputs += niceify(track_rp(sv, rp))
        outputs += '\n'

    if outputs.strip() != "":
        builder = ""

        def post_on_channel(built: str):
            logging.info('Posting on channel...')
            url = auth_data['hook']
            requests.post(
                url,
                json={
                    "content": built,
                    "username": "GradeWatcherBot"
                }
            )

        for line in outputs.splitlines():
            logging.info(line)
            if len(builder) + len(line) + 5 > 2000:
                post_on_channel(builder)
                builder = ""
            builder += line + "\n"


if __name__ == '__main__':
    main()
