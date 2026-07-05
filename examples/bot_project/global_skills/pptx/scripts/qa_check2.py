# -*- coding: utf-8 -*-
import zipfile, xml.etree.ElementTree as ET
import sys

sys.stdout.reconfigure(encoding='utf-8')

path = r'F:\tool\pythonProject\ModexAgent\examples\bot_project\skills\main\office-expert\pptx\output\Office助手自我介绍.pptx'
z = zipfile.ZipFile(path)
print('Total files:', len(z.namelist()))

# Extract text from slides
for i in range(1, 6):
    slide_xml = z.read(f'ppt/slides/slide{i}.xml')
    root = ET.fromstring(slide_xml)
    texts = []
    for t_elem in root.iter('{http://schemas.openxmlformats.org/drawingml/2006/main}t'):
        if t_elem.text:
            texts.append(t_elem.text.strip())
    print(f'\n=== Slide {i} ===')
    for t in texts:
        print(f'  {t}')
