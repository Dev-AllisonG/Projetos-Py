import pygame
import random
import time
import sys
import os


def resource_path(relative_path):
    try:
        base_path = sys._MEIPASS
    except Exception:
        base_path = os.path.abspath(".")
    return os.path.join(base_path, relative_path)


pygame.mixer.pre_init(44100, -16, 2, 512)
pygame.init()
pygame.mixer.init()

screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN)
WIDTH, HEIGHT = screen.get_size()

pygame.display.set_caption("...")

clock = pygame.time.Clock()


ascii_art = r"""
               #####    zero      #####             
             ########  gotch   ##### ##            
            ##    #####   u ###########            
           ## ###  ################# #            
           ## ####  ##################            
    ##     ## ### # ###################           
   #####   ### ##   ####################          
########   ## # ###################### #          
########    ## # ############ #######  #          
########     ## # ###########   ##### #           
  ##########  ##   ###########  #### ##           
   ############################## ### ###         
     ###########################   ## ##          
        ####################        ###           
           ###################   ########         
              ##############################      
                  ############################    
                     ##  ###   ################   
                      #        ################   
                      #        ###############    
                     ##         ############      
                      #         ############      
                      ##          #########       
                       #            ########      
                       ##            #########    
                        ##            ########    
                          ##          ########    
                           ###       #########    
                            #################     
                             ###############      
                              ###############     
                               ##############     
                                #############     
                                ############      
                               ############       
                                ##########        
                                 #########        
"""

font_ascii = pygame.font.SysFont("Consolas", 18)
font_big = pygame.font.SysFont("Arial", 80)
font_small = pygame.font.SysFont("Arial", 40)

start_time = time.time()
running = True
sound_played = False



def play_sounds():
    sound1 = pygame.mixer.Sound(resource_path("jumpscare1.wav"))
    sound2 = pygame.mixer.Sound(resource_path("jumpscare2.wav"))


    sound1.play()
    pygame.time.delay(1)
    sound2.play()
    pygame.time.delay(1)



def draw_ascii(surface, shake_x, shake_y):
    lines = ascii_art.split("\n")
    y_offset = 80

    for line in lines:
        text_surface = font_ascii.render(line, True, (255, 0, 0))
        surface.blit(
            text_surface,
            (WIDTH // 2 - 200 + shake_x, y_offset + shake_y)
        )
        y_offset += 20



def draw_glitch(surface, intensity):
    for _ in range(intensity):
        x = random.randint(0, WIDTH)
        y = random.randint(0, HEIGHT)
        w = random.randint(40, 300)
        h = random.randint(3, 12)

        glitch_rect = pygame.Rect(x, y, w, h)
        color = (
            random.randint(150, 255),
            random.randint(0, 60),
            random.randint(0, 60)
        )
        pygame.draw.rect(surface, color, glitch_rect)



def distortion(surface, intensity):
    for _ in range(intensity):
        slice_height = random.randint(5, 25)
        y = random.randint(0, HEIGHT - slice_height)

        slice_rect = pygame.Rect(0, y, WIDTH, slice_height)
        offset = random.randint(-60, 60)

        sub = surface.subsurface(slice_rect).copy()
        surface.blit(sub, (offset, y))



while running:

    elapsed = time.time() - start_time
    intensity = min(int(elapsed * 6), 80)

    screen.fill((0, 0, 0))

   
    shake_x = random.randint(-intensity, intensity)
    shake_y = random.randint(-intensity, intensity)

    
    draw_ascii(screen, shake_x, shake_y)

    
    text = font_big.render("EU ESTOU AQUI", True, (255, 0, 0))
    warning = font_small.render("VOCÊ NÃO PODE SAIR", True, (200, 0, 0))

    screen.blit(text, (WIDTH // 2 - 350 + shake_x, HEIGHT // 2 + shake_y))
    screen.blit(warning, (WIDTH // 2 - 250 + shake_x, HEIGHT // 2 + 100 + shake_y))

   
    draw_glitch(screen, intensity)

    
    distortion(screen, intensity // 3)

    
    if 5 < elapsed < 5.3:
        screen.fill((255, 255, 255))
        if not sound_played:
            play_sounds()
            sound_played = True

    pygame.display.update()
    clock.tick(60)

    if elapsed > 8:
        running = False

    for event in pygame.event.get():
        if event.type == pygame.QUIT:
            running = False
        if event.type == pygame.KEYDOWN:
            if event.key == pygame.K_ESCAPE:
                running = False

pygame.quit()
sys.exit()
