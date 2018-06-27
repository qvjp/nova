# Copyright 2013 IBM Corp.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.


from webob import exc

from nova.i18n import _

MIN_ATTRIBUTE_NAME = "min_count"
MAX_ATTRIBUTE_NAME = "max_count"
RRID_ATTRIBUTE_NAME = "return_reservation_id"


# NOTE(gmann): This function is not supposed to use 'body_deprecated_param'
# parameter as this is placed to handle scheduler_hint extension for V2.1.
def server_create(server_dict, create_kwargs, body_deprecated_param):
    # min_count and max_count are optional.  If they exist, they may come
    # in as strings.  Verify that they are valid integers and > 0.
    # Also, we want to default 'min_count' to 1, and default
    # 'max_count' to be 'min_count'.
    min_count = int(server_dict.get(MIN_ATTRIBUTE_NAME, 1))
    max_count = int(server_dict.get(MAX_ATTRIBUTE_NAME, min_count))
    return_id = server_dict.get(RRID_ATTRIBUTE_NAME, False)

    if min_count > max_count:
        msg = _('min_count must be <= max_count')
        raise exc.HTTPBadRequest(explanation=msg)

    create_kwargs['min_count'] = min_count
    create_kwargs['max_count'] = max_count
    create_kwargs['return_reservation_id'] = return_id
