# -*- coding: utf-8 -*-
import zipfile, xml.etree.ElementTree as ET

path = r'F:\tool\pythonProject\ModexAgent\examples\bot_project\skills\main\office-expert\pptx\output\Office助手自我介绍.pptx'
z = zipfile.ZipFile(path)

# Check slide layout references
for i in range(1, 6):
    slide_xml = z.read(f'ppt/slides/slide{i}.xml')
    root = ET.fromstring(slide_xml)
    
    # Get slide background if any
    bg = root.find('.//{http://schemas.openxmlformats.org/presentationml/2006/main}bg')
    has_bg = bg is not None
    
    # Check for solid fill colors
    srgb_colors = []
    for elem in root.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}srgbClr'):
        color_val = elem.get('val', '')
        srgb_colors.append(color_val)
    
    # Get font names
    fonts = set()
    for latin in root.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}latin'):
        font_name = latin.get('typeface', '')
        if font_name:
            fonts.add(font_name)
    
    print(f'Slide {i}: has_bg={has_bg}, colors={srgb_colors[:10]}, fonts={fonts}')
