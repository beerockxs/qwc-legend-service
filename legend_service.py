from io import BytesIO
import os

from PIL import Image
from flask import Response, send_file
import requests

from qwc_services_core.permissions_reader import PermissionsReader
from qwc_services_core.runtime_config import RuntimeConfig


PIL_Formats = {
    "image/bmp": "BMP",
    "application/postscript": "EPS",
    "image/gif": "GIF",
    "image/jpeg": "JPEG",
    "image/jp2": "JPEG 2000",
    "image/x-pcx": "PCX",
    "image/png": "PNG",
    "image/tiff": "TIFF",
    "image/webp": "WebP"
}


class LegendService:
    """LegendService class

    Provide legend graphics for WMS layers with custom legend images.
    Acts as a proxy to a QGIS server.
    """

    def __init__(self, tenant, logger):
        """Constructor

        :param Logger logger: Application logger
        """
        self.tenant = tenant
        self.logger = logger

        config_handler = RuntimeConfig("legend", logger)
        config = config_handler.tenant_config(tenant)

        # get internal QGIS server URL from config
        self.qgis_server_url = config.get(
            'default_qgis_server_url', 'http://localhost:8001/ows/'
        ).rstrip('/') + '/'

        # get path to legend images from config
        self.legend_images_path = config.get('legend_images_path', 'legends/')

        self.resources = self.load_resources(config)
        self.permissions_handler = PermissionsReader(tenant, logger)

    def get_legend(self, mapid, layer_param, format_param, params, type,
                   access_token, identity):
        """Return legend graphic for specified layer.

        :param str mapid: WMS service name
        :param str layer_param: WMS layer names
        :param str format_param: Image format
        :param dict params: Other params to forward to QGIS Server
        :param str type: The legend image type, either "default", "thumbnail" or "tooltip".
        :param obj identity: User identity
        """
        if not self.wms_permitted(mapid, identity):
            # map unknown or not permitted
            return self.service_exception(
                'MapNotDefined',
                'Map "%s" does not exist or is not permitted' % mapid
            )

        if format_param not in PIL_Formats:
            self.logger.warning(
                "Unsupported format requested, falling back to image/png"
            )
            format_param = "image/png"

        # get permitted resources
        requested_layers = layer_param.split(',')
        permitted_resources = self.permitted_resources(mapid, identity)
        permitted_layers = permitted_resources['permitted_layers']
        public_layers = permitted_resources['public_layers']
        group_layers = permitted_resources['groups_to_expand']
        # filter layers by permissions
        requested_layers = [
            layer for layer in requested_layers
            if layer in public_layers
        ]
        # replace group layers containing custom legends with permitted
        # sublayers
        expanded_layers = self.expand_group_layers(
            requested_layers, group_layers, permitted_layers
        )

        self.logger.debug("Requested layers: %s" % requested_layers)
        self.logger.debug("Expanded layers:  %s" % expanded_layers)

        dpi = params.get('dpi')
        imgdata = []
        for layer in expanded_layers:
            legend_image = self.get_legend_image(mapid, layer, type)
            if legend_image is not None:
                if dpi and dpi != '90':
                    try:
                        # scale image to requested DPI
                        img = Image.open(BytesIO(legend_image))
                        scale = float(dpi) / 90.0
                        new_size = (
                            int(img.width * scale), int(img.height * scale)
                        )
                        img = img.resize(new_size, Image.ANTIALIAS)
                        output = BytesIO()
                        img.save(output, PIL_Formats[format_param])
                        imgdata.append({"data": output, "format": None})
                    except Exception as e:
                        self.logger.error(
                            "Could not resize image for %s:\n%s" % (layer, e)
                        )
                        imgdata.append(
                            {"data": BytesIO(legend_image), "format": None}
                        )
                else:
                    imgdata.append({
                        "data": BytesIO(legend_image), "format": None
                    })
            else:
                req_params = {
                    "service": "WMS",
                    "version": "1.3.0",
                    "request": "GetLegendGraphic",
                    "layer": layer,
                    "format": format_param,
                    "style": ""
                }
                req_params.update(params)

                headers = {}
                if access_token:
                    headers['Authorization'] = "Bearer " + access_token

                response = requests.get(
                    self.qgis_server_url + mapid, params=req_params,
                    headers=headers, timeout=10
                )
                self.logger.debug("Forwarding request to %s" % response.url)

                if response.content.startswith(b'<ServiceExceptionReport'):
                    self.logger.warning(response.content)
                elif response.status_code == 200:
                    buf = BytesIO()
                    buf.write(response.content)
                    imgdata.append({"data": buf, "format": format_param})
                else:
                    # Empty image in case of server error
                    output = BytesIO()
                    Image.new("RGB", (1, 1), (255, 255, 255)).save(
                        output, PIL_Formats[format_param]
                    )
                    imgdata.append({"data": output, "format": format_param})

        if len(imgdata) == 0:
            # layer not found or faulty
            return self.service_exception(
                'LayerNotDefined',
                'Layer "%s" does not exist or is not permitted' % layer_param
            )

        # If just one image, return it
        elif len(imgdata) == 1:
            # Convert to requested format if necessary
            if imgdata[0]["format"] != format_param:
                output = BytesIO()
                try:
                    imgdata[0]["data"].seek(0)
                    Image.open(imgdata[0]["data"]).save(
                        output, PIL_Formats[format_param]
                    )
                except:
                    # Empty 1x1 image
                    Image.new("RGB", (1, 1), (255, 255, 255)).save(
                        output, PIL_Formats[format_param]
                    )
                output.seek(0)
                imgdata[0]["data"] = output

            imgdata[0]["data"].seek(0)
            return send_file(imgdata[0]["data"], mimetype=format_param)

        # Otherwise, compose images
        width = 0
        height = 0
        for entry in imgdata:
            try:
                entry["image"] = Image.open(entry["data"])
                width = max(width, entry["image"].width)
                height += entry["image"].height
            except:
                entry["image"] = None

        image = Image.new("RGBA", (width, height), (255, 255, 255, 255))
        y = 0
        for entry in imgdata:
            if entry["image"]:
                image.paste(entry["image"], (0, y))
                y += entry["image"].height

        data = BytesIO()
        image.save(data, PIL_Formats[format_param])
        data.seek(0)
        return send_file(data, mimetype=format_param)

    def service_exception(self, code, message):
        """Create ServiceExceptionReport XML response

        :param str code: ServiceException code
        :param str message: ServiceException text
        """
        return Response(
            (
                '<ServiceExceptionReport version="1.3.0">\n'
                ' <ServiceException code="%s">%s</ServiceException>\n'
                '</ServiceExceptionReport>'
                % (code, message)
            ),
            content_type='text/xml; charset=utf-8',
            status=200
        )

    def expand_group_layers(self, requested_layers, groups_to_expand,
                            permitted_layers):
        """Recursively filter layers by permissions and replace group layers
        with permitted sublayers and return resulting layer list.

        :param list(str) requested_layers: List of requested layer names
        :param obj groups_to_expand: Lookup for group layers with sublayers
                                     that have custom legends or are restricted
        :param list(str) permitted_layers: List of permitted layer names
        """
        expanded_layers = []

        for layer in requested_layers:
            if layer in permitted_layers:
                if layer in groups_to_expand:
                    # expand sublayers
                    sublayers = []
                    for sublayer in groups_to_expand.get(layer):
                        if sublayer in permitted_layers:
                            sublayers.append(sublayer)

                    expanded_layers += self.expand_group_layers(
                        sublayers, groups_to_expand, permitted_layers
                    )
                else:
                    # leaf layer or full group layer
                    expanded_layers.append(layer)

        return expanded_layers

    def get_legend_image(self, mapid, layer, type):
        """Return any custom legend image for a layer.

        :param str mapid: Service name
        :param str layer: WMS Layer name
        :param str type: Legend image type (default|thumbnail|tooltip)
        """
        image_data = None

        # get lookup for custom legend images
        legend_images = self.resources['wms_services'][mapid]['legend_images']
        if layer not in legend_images:
            # layer has no custom legend image
            return None

        # TODO: legend image types

        try:
            image_path = os.path.join(
                self.legend_images_path, legend_images[layer]
            )
            if os.path.isfile(image_path):
                self.logger.debug(
                    "Loading legend image '%s' for layer '%s'" %
                    (image_path, layer)
                )
                # load image file
                with open(image_path, 'rb') as f:
                    image_data = f.read()
            else:
                self.logger.warning(
                    "Could not find legend image '%s' for layer '%s'" %
                    (image_path, layer)
                )
        except Exception as e:
            self.logger.error(
                "Could not load legend image '%s' for layer '%s'" %
                (image_path, layer)
            )

        return image_data

    def load_resources(self, config):
        """Load service resources from config.

        :param RuntimeConfig config: Config handler
        """
        wms_services = {}

        # collect service resources
        for wms in config.resources().get('wms_services', []):
            # collect WMS layers
            resources = {
                # root layer name
                'root_layer': wms['root_layer']['name'],
                # public layers without hidden sublayers: [<layers>]
                'public_layers': [],
                # available layers including hidden sublayers: [<layers>]
                'available_layers': [],
                # lookup for complete group layers
                # sub layers ordered from top to bottom:
                #     {<group>: [<sub layers]}
                'group_layers': {},
                # lookup for group layers containing layers with
                # custom legend images
                # sub layers ordered from top to bottom:
                #     {<group>: [<sub layers]}
                'groups_to_expand': {},
                # lookup for layers with custom legend images:
                #     {<layer>: <legend img>}
                'legend_images': {}
            }
            self.collect_layers(wms['root_layer'], resources, False)

            wms_services[wms['name']] = resources

        return {
            'wms_services': wms_services
        }

    def collect_layers(self, layer, resources, hidden):
        """Recursively collect layer info for layer subtree from config.

        :param obj layer: Layer or group layer
        :param obj resources: Partial lookups for layer resources
        :param bool hidden: Whether layer is a hidden sublayer
        """
        if not hidden:
            resources['public_layers'].append(layer['name'])
        resources['available_layers'].append(layer['name'])

        if layer.get('layers'):
            # group layer

            hidden |= layer.get('hide_sublayers', False)
            sublayers_have_custom_legend = False

            # collect sublayers
            sublayers = []
            for sublayer in layer['layers']:
                sublayers.append(sublayer['name'])
                # recursively collect sublayer
                self.collect_layers(sublayer, resources, hidden)
                if (
                    sublayer['name'] in resources['legend_images'] or
                    sublayer['name'] in resources['groups_to_expand']
                ):
                    # sublayer has custom legend image
                    # or is a group containing such sublayers
                    sublayers_have_custom_legend |= True

            resources['group_layers'][layer['name']] = sublayers

            if layer.get('hide_sublayers') and layer.get('legend_image'):
                # set custom legend image for group with hidden sublayers
                # Note: overrides any custom legend image of sublayers
                resources['legend_images'][layer['name']] = \
                    layer.get('legend_image')
            elif sublayers_have_custom_legend:
                # group has sublayer with custom legend image
                resources['groups_to_expand'][layer['name']] = sublayers
        else:
            # layer
            if layer.get('legend_image'):
                # set custom legend image
                resources['legend_images'][layer['name']] = \
                    layer.get('legend_image')

    def wms_permitted(self, service_name, identity):
        """Return whether WMS is available and permitted.

        :param str service_name: Service name
        :param obj identity: User identity
        """
        if self.resources['wms_services'].get(service_name):
            # get permissions for WMS
            wms_permissions = self.permissions_handler.resource_permissions(
                'wms_services', identity, service_name
            )
            if wms_permissions:
                return True

        return False

    def permitted_resources(self, service_name, identity):
        """Return permitted resources for a legend service.

        :param str service_name: Service name
        :param obj identity: User identity
        """
        if not self.resources['wms_services'].get(service_name):
            # WMS service unknown
            return {}

        # get permissions for WMS
        wms_permissions = self.permissions_handler.resource_permissions(
            'wms_services', identity, service_name
        )
        if not wms_permissions:
            # WMS not permitted
            return {}

        wms_resources = self.resources['wms_services'][service_name].copy()

        # get available layers
        available_layers = wms_resources['available_layers']

        # combine permissions
        permitted_layers = set()
        for permission in wms_permissions:
            for layer in permission['layers']:
                name = layer['name']
                if name in available_layers:
                    permitted_layers.add(name)

        # filter by permissions

        # public layers
        public_layers = [
            layer for layer in wms_resources['public_layers']
            if layer in permitted_layers
        ]

        # collect restricted group layers
        restricted_group_layers = {}
        self.collect_restricted_group_layers(
            wms_resources['root_layer'], wms_resources['group_layers'],
            permitted_layers, restricted_group_layers
        )
        # merge with groups to expand
        groups_to_expand = wms_resources['groups_to_expand']
        for group, allowed_sublayers in restricted_group_layers.items():
            # update with allowed layers
            groups_to_expand[group] = allowed_sublayers

        return {
            'permitted_layers': sorted(list(permitted_layers)),
            'public_layers': public_layers,
            'groups_to_expand': groups_to_expand
        }

    def collect_restricted_group_layers(self, layer, group_layers,
                                        permitted_layers,
                                        restricted_group_layers):
        """Recursively collect group layers with restricted sublayers.

        :param str layer: Layer name
        :param obj group_layers: Lookup for group layers
        :param list(str) permitted_layers: List of permitted layer names
        :Param obj restricted_group_layers: Partial lookup for restricted
                                            group layers
        """
        if layer in group_layers:
            # group layer

            # collect sublayers
            sublayers = []
            sublayers_restricted = False
            for sublayer in group_layers[layer]:
                if sublayer in permitted_layers:
                    # add permitted layer
                    sublayers.append(sublayer)

                # recursively collect sublayer
                self.collect_restricted_group_layers(
                    sublayer, group_layers, permitted_layers,
                    restricted_group_layers
                )
                if (
                    sublayer not in permitted_layers or
                    sublayer in restricted_group_layers
                ):
                    # sublayer is restricted
                    # or is a group containing such sublayers
                    sublayers_restricted |= True

            if sublayers_restricted:
                # group has restricted sublayers
                restricted_group_layers[layer] = sublayers
