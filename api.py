import os
import random
import re
import shutil
import subprocess
import tempfile
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
import asyncio
import aiohttp
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from datetime import datetime, timedelta
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()


# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Replace with the appropriate list of allowed origins
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Set default output file name and directory
output_dir = 'downloads/'
output_format = '.pdf'

# Set maximum number of slides to download
max_slides = 150

# Set socket timeout
import socket

socket.setdefaulttimeout(20)

# Mount the '/downloads' directory as a static file directory
app.mount('/downloads', StaticFiles(directory='downloads'), name='static')

# Cache expiration time (in seconds)
cache_expiration = 20  # 1 hour

# Cache to store downloaded PDF files and their expiration time
pdf_cache = {}


async def download_slide(session, i, url, dir_tmp):
    # Form slide data
    remote_slide = url
    local_slide = os.path.join(dir_tmp, f'slide-{str(i)}.jpg')

    # Download slide
    try:
        async with session.get(remote_slide) as response:
            with open(local_slide, 'wb') as f:
                f.write(await response.read())
    except aiohttp.ClientConnectorError as e:
        # Cleanup and return error
        shutil.rmtree(dir_tmp)
        raise HTTPException(status_code=500, detail=f'Could not download slide-{str(i)} from {url}. {str(e)}')
    except Exception as e:
        # Cleanup and return error
        shutil.rmtree(dir_tmp)
        raise HTTPException(status_code=500, detail=f'Could not download slide-{str(i)}. {str(e)}')
    else:
        # Add to array
        return local_slide


@app.post('/convert')
async def convert_slideshare(request_data: dict):
    global output_dir  # Declare output_dir as global

    # Get input URL from the request body
    url = request_data.get('url')
    if not url:
        raise HTTPException(status_code=400, detail='URL not provided')

    # Check if the PDF is already in the cache
    if url in pdf_cache:
        cached_pdf = pdf_cache[url]
        if datetime.now() < cached_pdf['expiration']:
            pdf_content = base64.b64decode(cached_pdf['pdf_content'])
            return Response(content=pdf_content, media_type='application/pdf')

    # If output path was provided, split it into file name and directory
    output_path = request_data.get('output_path')
    if output_path:
        output_dir, output_file = os.path.split(output_path)
    else:
        output_file = ''

    # Check output file name
    if output_file == '':
        # Build output file name from URL
        url_match = re.search('(?:[^\/]*\/){3}([A-Za-z0-9-_\.]*)(?:\/)([A-Za-z0-9-_\.]*)', url)
        output_file = f'{url_match.group(2)}-by-{url_match.group(1)}{output_format}'
    else:
        # Check if correct format
        if output_file[-4:] != output_format:
            output_file = f'{output_file}{output_format}'

    # Check output directory
    if output_dir != '':
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError:
            if not os.path.isdir(output_dir):
                raise HTTPException(status_code=400, detail='Invalid output directory')

    # (Re)build output path
    output_path = os.path.join(output_dir, output_file)

    # Create temporary directory
    dir_tmp = tempfile.mkdtemp()

    # Grab SlideShare HTML
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                html = await response.text()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f'Could not fetch SlideShare HTML. {str(e)}')

    # Read HTML and get images
    soup = BeautifulSoup(html, 'html.parser')
    slide_container = soup.find('div', attrs={'data-testid': 'slide-container'})
    images = []  # Initialize images as an empty list
    if slide_container:
        pictures = slide_container.find_all('picture', attrs={'data-testid': 'slide-image-picture'})
        if pictures:
            for picture in pictures:
                source_tag = picture.find('source', attrs={'data-testid': 'slide-image-source'})
                if source_tag:
                    srcset = source_tag['srcset']
                    urls = re.findall(r'(\S+)\s\d+w', srcset)
                    images.append(urls)
        else:
            img_tags = slide_container.find_all('img', attrs={'data-testid': 'slide-image'})
            if img_tags:
                for img_tag in img_tags:
                    srcset = img_tag['srcset']
                    urls = re.findall(r'(\S+)\s\d+w', srcset)
                    images.append(urls)

    # Check if slides found
    if not images:
        raise HTTPException(status_code=404, detail='No slides found')

    # Choose the version of the image to download
    image_version = request_data.get('image_version')
    if image_version:
        choice_index = image_version - 1
    else:
        choice_index = 0  # Default to the first version

    # Limit the number of slides
    selected_urls = [image_urls[choice_index] for image_urls in images]
    selected_urls = selected_urls[:max_slides]

    # Download slides to temporary directory
    downloaded_slides = []
    async with aiohttp.ClientSession() as session:
        tasks = []
        for i, url in enumerate(selected_urls):
            task = asyncio.ensure_future(download_slide(session, i + 1, url, dir_tmp))
            tasks.append(task)
        responses = await asyncio.gather(*tasks)
        downloaded_slides = responses

    # Combine images into PDF
    # Compress images
    with ThreadPoolExecutor() as executor:
        def compress_image(slide_path):
            subprocess.call(f'convert {slide_path} -quality 75 {slide_path}', shell=True)

        compression_tasks = [executor.submit(compress_image, slide_path) for slide_path in downloaded_slides]
        for future in as_completed(compression_tasks):
            try:
                future.result()
            except Exception as e:
                shutil.rmtree(dir_tmp)
                raise HTTPException(status_code=500, detail=f'Could not compress images. {str(e)}')

    downloaded_slides_str = ' '.join(downloaded_slides)
    try:
        imagick = 'convert'
        subprocess.call(f'{imagick} {downloaded_slides_str} -quality 100 {output_path}', shell=True)
    except Exception as ex:
        shutil.rmtree(dir_tmp)
        raise HTTPException(status_code=500, detail=f'Could not convert slides to PDF. {str(ex)}')

    # Remove temporary directory
    shutil.rmtree(dir_tmp)

    # Check if file was created
    if os.path.isfile(output_path):
        # Read the PDF file
        with open(output_path, 'rb') as f:
            pdf_content = f.read()
            pdf_content_base64 = base64.b64encode(pdf_content).decode()

        # Cache the PDF file
        cache_expiration_time = datetime.now() + timedelta(seconds=cache_expiration)
        pdf_cache[url] = {
            'pdf_content': pdf_content_base64,
            'expiration': cache_expiration_time
        }

        # Set response headers to force download
        headers = {
            'Content-Disposition': f'attachment; filename="{output_file}"',
            'Content-Type': 'application/pdf',
        }
        return Response(content=pdf_content, media_type='application/pdf', headers=headers)
    else:
        raise HTTPException(status_code=500, detail='File could not be created')


async def simulate_concurrent_requests():
    # Simulate 1000 concurrent requests
    tasks = []

    urls = [
        "https://www.slideshare.net/davekerst/company-intro-11142008",
        "https://www.slideshare.net/ivoapostolov/are-you-swimming-with-the-big-fishes-presentation",
        "https://www.slideshare.net/reneesoucy/toy-fair-20072009-presentation-756073",
        "https://www.slideshare.net/paul_senior/Effective-and-Efficient-Talent-Acquisition-Management",
        "https://www.slideshare.net/mhannock/memphis-emarketing-group-presentation",
        "https://www.slideshare.net/guest832dec9/brand-plateform-presentation-756100",
        "https://www.slideshare.net/vanrompay/the-succes-story-of-a-recruiter-finding-a-turnaround-executive-presentation",
        "https://www.slideshare.net/mperez49/romeoandjuliet1-presentation",
        "https://www.slideshare.net/rvoltz/iasb-08-presentation",
        "https://www.slideshare.net/coachbriany/nnet-presentation",
        "https://www.slideshare.net/mikeyk/open-source-presentation-756133",
        "https://www.slideshare.net/Larsip/cinematography97-2003-presentation",
        "https://www.slideshare.net/nadimissimple/bioinformatics-presentation-756154",
        "https://www.slideshare.net/erobak/Pluris-FAS-157-Handbook-November-2008",
        "https://www.slideshare.net/lisa1582/searching-google-presentation",
        "https://www.slideshare.net/JoomlaChicago/social-networking-presentation-presentation",
        "https://www.slideshare.net/fa6o0om/smoking-756202",
        "https://www.slideshare.net/adfigueiredo/toward-an-epistemology-of-engineering-presentation",
        "https://www.slideshare.net/GallagherPreach/six-steps-to-spiritual-restoration-part-3-presentation",
        "https://www.slideshare.net/nadimissimple/bioinformatics-presentation-756230",
        "https://www.slideshare.net/SSE/school-for-social-entrepreneurs-overview-presentation",
        "https://www.slideshare.net/hstulberg/buz-comment-card-system-presentation",
        "https://www.slideshare.net/asloman/virtual-machines-in-philosophy-engineering-biology-at-wpe-2008",
        "https://www.slideshare.net/alex_dc/social-media-is-presentation",
        "https://www.slideshare.net/Lynne_martell/the-first-thanksgiving-presentation",
        "https://www.slideshare.net/sitapati/3-types-of-happiness-presentation",
        "https://www.slideshare.net/willebil/joomladay-switzerland-joomla-15-and-roadmap-to-future-versions-presentation",
        "https://www.slideshare.net/guestd89d166/80k-art-school-education-presentation",
        "https://www.slideshare.net/pgm617/Pres-Demov2",
        "https://www.slideshare.net/willebil/joomladay-switzerland-security-presentation",
        "https://www.slideshare.net/melinda.brooks/C21Loop",
        "https://www.slideshare.net/niccoletaylor/higher-maths-121-sets-and-functions-1205778086374356-2-presentation",
        "https://www.slideshare.net/choconancy/stewarding-technology-for-communities-on-learn08final-presentation",
        "https://www.slideshare.net/ranardel/the-augustan-age-jonathan-swift-presentation",
        "https://www.slideshare.net/kevmille/using-ubuntu-linux-at-the-american-international-school-presentation",
        "https://www.slideshare.net/aroongudibande/aroon-gudibandes-photo-presentation-presentation",
        "https://www.slideshare.net/Samanthajday/culminating-statement-presentation-756444",
        "https://www.slideshare.net/emmapersky/emmas-guide-to-great-barcamping-presentation",
        "https://www.slideshare.net/kmatthews/social-media-for-healthcare-presentation",
    ]

    for url in urls:
        task = asyncio.ensure_future(convert_slideshare({
            'url': url,
            'image_version': random.choice([1, 2, 3])
        }))
        tasks.append(task)
    responses = await asyncio.gather(*tasks)
    return responses


@app.get('/test')
async def test_concurrent_requests():
    responses = await simulate_concurrent_requests()
    return {'message': 'Concurrent requests completed', 'responses': responses}


if __name__ == '__main__':
    # Create the 'downloads' directory if it doesn't exist
    os.makedirs('downloads', exist_ok=True)

    # Run the FastAPI application
    import uvicorn

    uvicorn.run(app, host='0.0.0.0', port=8000)
