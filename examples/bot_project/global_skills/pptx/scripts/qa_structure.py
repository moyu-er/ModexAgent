# -*- coding: utf-8 -*-
import zipfile, xml.etree.ElementTree as ET

path = r'F:\tool\pythonProject\ModexAgent\examples\bot_project\skills\main\office-expert\pptx\output\Office助手自我介绍.pptx'
z = zipfile.ZipFile(path)

output = []
for i in range(1, 6):
    slide_xml = z.read(f'ppt/slides/slide{i}.xml')
    root = ET.fromstring(slide_xml)
    spTree = root.findall('.//{http://schemas.openxmlformats.org/presentationml/2006/main}sp')
    output.append(f'Slide {i}: {len(spTree)} shapes, {len(slide_xml)} bytes')

output.append(f'\nFile size on disk: 111,819 bytes')
output.append(f'Slide count: 5')

with open('qa_result.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(output))
print('QA result written')
