#!/usr/bin/env python3
"""A small, stateful, in-memory FAKE Mealie server for local development.

    python dev/fake_mealie.py --port 9925
    python dev/fake_mealie.py --port 9925 --recipes 450     # big library for pagination

It imitates the parts of the Mealie v2/v3 REST API that `mealie_client` uses
(see dev/README.md for the list and for known divergences from real Mealie).
All state lives in memory and is rebuilt from a deterministic seed at startup
and on `POST /__reset`.
"""
from __future__ import annotations

import argparse
import colorsys
import hashlib
import html
import json
import math
import random
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode, urlparse

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, Response

GROUP_ID = "0b4e7a1c-5d2f-4c3a-9e8b-1f2a3b4c5d6e"
HOUSEHOLD_ID = "7c9d1e2f-3a4b-4c5d-8e6f-a1b2c3d4e5f6"
USER_ID = "3f2e1d0c-9b8a-4f6e-a5d4-c3b2a1f0e9d8"
VERSION = "v3.0.0-fake"

# --------------------------------------------------------------------------- #
# Seed data
# --------------------------------------------------------------------------- #
# (name, cuisine, servings, effort or None, description, ingredients, steps, raw)
#   ingredients: ';'-separated lines.  steps: '|'-separated.
#   raw=True  -> recipe looks like a fresh un-parsed import: every ingredient is
#                note-only (quantity 0, no unit/food), so `update_recipe_ingredients`
#                has real work to do.
#   raw=False -> lines starting with a number are stored structured
#                (quantity/unit/food/note); the others stay note-only.
_D = "dinner"
SEED_RECIPES: list[tuple] = [
    ("Butter Chicken", "Indian", 4, 3, "Mild, creamy tomato curry the kids will actually eat.",
     "600 g chicken thighs, diced;2 tbsp butter;1 onion, finely chopped;3 cloves garlic, crushed;1 tbsp garam masala;1 can chopped tomatoes;150 ml cream;2 cups basmati rice;Fresh coriander, to serve",
     "Brown the chicken in butter and set aside.|Soften the onion and garlic, then stir in the garam masala.|Add tomatoes and simmer for 15 minutes.|Return the chicken, stir in the cream and simmer until cooked through.|Serve over rice with coriander.", False),
    ("Beef Tacos", "Mexican", 4, 2, "Build-your-own tacos with spiced mince and crunchy salad.",
     "500 g beef mince;1 onion, diced;2 tsp ground cumin;1 tsp smoked paprika;1 can black beans, drained;8 taco shells;1 cup grated cheese;2 tomatoes, diced;Shredded lettuce, to serve;Sour cream, to serve",
     "Fry the onion and mince until browned.|Add spices and beans with a splash of water and simmer 10 minutes.|Warm the taco shells in the oven.|Serve with cheese, tomato, lettuce and sour cream.", False),
    ("Lamb Koftas", "Middle Eastern", 4, 3, "Grilled spiced lamb koftas with flatbread and yoghurt sauce.",
     "500 g lamb mince;1 onion, grated;2 cloves garlic, crushed;2 tsp ground cumin;1 tsp ground coriander;4 flatbreads;1 cup greek yoghurt;1 cucumber, diced;Mint leaves, to serve",
     "Mix the mince, onion, garlic and spices and shape onto skewers.|Grill for 10-12 minutes, turning often.|Stir cucumber and mint through the yoghurt.|Serve koftas in warm flatbreads with the sauce.", False),
    ("Fish Pie", "British", 6, 4, "Creamy smoked fish pie with a golden mash top.",
     "600 g white fish fillets, cubed;200 g smoked fish, flaked;1 kg potatoes, peeled;50 g butter;2 tbsp flour;500 ml milk;1 cup frozen peas;3 eggs, hard boiled;1 cup grated cheese;Salt and pepper, to taste",
     "Boil and mash the potatoes with half the butter.|Make a white sauce with remaining butter, flour and milk.|Fold fish, peas and quartered eggs into the sauce and tip into a dish.|Top with mash and cheese.|Bake at 190C for 30 minutes until golden.", False),
    ("Spaghetti Bolognese", "Italian", 6, 2, "The weeknight classic. Freezes well.",
     "500 g beef mince;1 onion, diced;2 carrots, grated;3 cloves garlic, crushed;2 cans chopped tomatoes;2 tbsp tomato paste;1 tsp dried oregano;500 g spaghetti;Parmesan, to serve",
     "Brown the mince with onion, carrot and garlic.|Add tomatoes, paste and oregano and simmer for 30 minutes.|Cook the spaghetti until al dente.|Toss together and serve with parmesan.", False),
    ("Chicken Stir Fry", "Chinese", 4, 2, "Fast veggie-loaded stir fry with a honey soy sauce.",
     "500 g chicken breast, sliced;1 head broccoli, cut into florets;1 red capsicum, sliced;2 carrots, sliced;3 tbsp soy sauce;1 tbsp honey;2 cloves garlic, crushed;1 tbsp fresh ginger, grated;2 cups jasmine rice",
     "Cook the rice.|Stir fry chicken over high heat until golden, then remove.|Stir fry the vegetables for 3-4 minutes.|Return the chicken with soy, honey, garlic and ginger and toss until glossy.", False),
    ("Sausage and Vegetable Tray Bake", "New Zealand", 4, 1, "Everything on one tray, in the oven, done.",
     "8 pork sausages;500 g potatoes, cubed;2 carrots, cut into chunks;1 red onion, cut into wedges;1 red capsicum, cut into chunks;2 tbsp olive oil;1 tsp dried thyme;Salt and pepper, to taste",
     "Heat the oven to 200C.|Toss the vegetables with oil and thyme on a large tray.|Nestle in the sausages.|Roast for 40 minutes, turning once.", False),
    ("Thai Green Chicken Curry", "Thai", 4, 3, "Fragrant coconut curry, as mild or as hot as you like.",
     "500 g chicken thighs, sliced;2 tbsp green curry paste;1 can coconut milk;1 cup green beans, trimmed;1 red capsicum, sliced;1 tbsp fish sauce;1 tsp brown sugar;2 cups jasmine rice;Thai basil, to serve",
     "Fry the curry paste for a minute until fragrant.|Add the chicken and coat well.|Pour in coconut milk and simmer for 10 minutes.|Add vegetables, fish sauce and sugar and cook 5 minutes more.|Serve over rice.", False),
    ("Roast Chicken with Vegetables", "British", 6, 4, "Sunday roast with crispy potatoes and gravy.",
     "1 whole chicken;1 kg potatoes, halved;500 g pumpkin, cut into chunks;3 carrots, halved;1 lemon, halved;4 cloves garlic;2 tbsp olive oil;2 sprigs rosemary;2 tbsp gravy powder",
     "Heat the oven to 190C.|Stuff the chicken with lemon, garlic and rosemary and rub with oil.|Roast for 1 hour 20 minutes, adding the vegetables after 30 minutes.|Rest the chicken for 15 minutes.|Make gravy with the pan juices.", False),
    ("Beef Stroganoff", "Russian", 4, 3, "Tender beef strips in a creamy mushroom sauce.",
     "500 g beef schnitzel, sliced;250 g mushrooms, sliced;1 onion, sliced;2 tsp smoked paprika;1 tbsp tomato paste;1 cup beef stock;150 g sour cream;400 g egg noodles;Chopped parsley, to serve",
     "Sear the beef in batches and set aside.|Cook onion and mushrooms until golden.|Stir in paprika, paste and stock and simmer 5 minutes.|Return the beef and stir through sour cream.|Serve over noodles.", False),
    ("Pumpkin and Feta Risotto", "Italian", 4, 3, "Oven-roasted pumpkin stirred through creamy risotto.",
     "500 g pumpkin, diced;1 onion, finely chopped;2 cloves garlic, crushed;2 cups arborio rice;1 litre vegetable stock;100 g feta, crumbled;2 handfuls baby spinach;2 tbsp olive oil",
     "Roast the pumpkin at 200C for 25 minutes.|Soften onion and garlic, then toast the rice for a minute.|Add hot stock a ladle at a time, stirring, until creamy.|Fold through pumpkin, spinach and feta.", False),
    ("Fish and Chips", "New Zealand", 4, 3, "Oven-baked crumbed fish with chunky chips.",
     "600 g white fish fillets;1 kg potatoes, cut into chips;1 cup panko breadcrumbs;2 eggs, beaten;0.5 cup flour;2 tbsp olive oil;1 lemon, cut into wedges;Tartare sauce, to serve",
     "Toss chips in oil and bake at 220C for 35 minutes.|Coat fish in flour, egg and crumbs.|Bake the fish for the final 15 minutes.|Serve with lemon and tartare sauce.", False),
    ("Chicken Enchiladas", "Mexican", 6, 3, "Cheesy baked enchiladas with shredded chicken.",
     "500 g chicken breast;8 tortillas;1 can black beans, drained;1 cup corn kernels;2 cups enchilada sauce;2 cups grated cheese;1 onion, diced;1 tsp ground cumin",
     "Poach and shred the chicken.|Mix chicken with beans, corn, onion, cumin and half the sauce.|Roll in tortillas and pack into a baking dish.|Top with remaining sauce and cheese.|Bake at 190C for 25 minutes.", False),
    ("Lamb Shanks", "New Zealand", 4, 5, "Slow-braised lamb shanks in red wine. Worth the wait.",
     "4 lamb shanks;2 onions, diced;3 carrots, diced;4 cloves garlic, crushed;2 cups beef stock;1 cup red wine;1 can chopped tomatoes;2 sprigs rosemary;1 kg potatoes, for mash",
     "Brown the shanks well on all sides.|Soften the vegetables in the same pot.|Add wine, stock, tomatoes and rosemary and return the shanks.|Cover and braise at 160C for 3 hours.|Serve on mash with the reduced sauce.", False),
    ("Macaroni Cheese", "American", 6, 2, "Proper baked mac and cheese with a crunchy top.",
     "500 g macaroni;50 g butter;3 tbsp flour;750 ml milk;3 cups grated cheese;1 tsp dijon mustard;4 rashers bacon, chopped;0.5 cup panko breadcrumbs",
     "Cook the macaroni.|Make a cheese sauce with butter, flour, milk, mustard and most of the cheese.|Fry the bacon until crisp.|Combine, top with crumbs and remaining cheese, and bake at 200C for 20 minutes.", False),
    ("Teriyaki Salmon", "Japanese", 4, 2, "Sticky glazed salmon with rice and greens.",
     "4 salmon fillets;4 tbsp soy sauce;2 tbsp honey;1 tbsp rice vinegar;1 tbsp fresh ginger, grated;2 cups sushi rice;2 heads bok choy, halved;1 tbsp sesame seeds",
     "Whisk soy, honey, vinegar and ginger together.|Pan fry the salmon skin-side down for 4 minutes.|Flip, add the sauce and bubble until sticky.|Serve with rice and steamed bok choy, sprinkled with sesame.", False),
    ("Mince and Cheese Pie", "New Zealand", 6, 4, "A family-sized version of the Kiwi bakery staple.",
     "500 g beef mince;1 onion, diced;2 tbsp flour;1 cup beef stock;1 tbsp worcestershire sauce;2 sheets puff pastry;1 cup grated cheese;1 egg, beaten",
     "Brown the mince and onion.|Stir in flour, then stock and worcestershire, and simmer until thick. Cool.|Line a pie dish with pastry, fill with mince and top with cheese.|Add a pastry lid and brush with egg.|Bake at 200C for 30 minutes.", False),
    ("Vegetable Fried Rice", "Chinese", 4, 1, "Use-it-up fried rice. Best with day-old rice.",
     "4 cups cooked rice;2 eggs, beaten;1 cup frozen peas;2 carrots, diced;1 cup corn kernels;3 tbsp soy sauce;1 tbsp sesame oil;3 spring onions, sliced",
     "Scramble the eggs in a hot wok and set aside.|Stir fry the vegetables for 3 minutes.|Add the rice and toss until hot.|Stir through soy, sesame oil, egg and spring onion.", False),
    ("Chicken Schnitzel", "German", 4, 2, "Crispy crumbed chicken with slaw.",
     "4 chicken breasts, flattened;1 cup panko breadcrumbs;2 eggs, beaten;0.5 cup flour;3 tbsp olive oil;0.25 head cabbage, shredded;2 carrots, grated;3 tbsp mayonnaise;1 lemon, cut into wedges",
     "Coat the chicken in flour, egg and crumbs.|Shallow fry for 4 minutes each side until golden.|Toss cabbage and carrot with mayonnaise.|Serve with lemon wedges.", False),
    ("Slow Cooker Pulled Pork", "American", 6, 2, "Set and forget. Pile into buns with slaw.",
     "1.5 kg pork shoulder;1 cup barbecue sauce;2 tbsp brown sugar;1 tbsp smoked paprika;1 onion, sliced;6 burger buns;0.25 head cabbage, shredded;3 tbsp mayonnaise",
     "Rub the pork with sugar and paprika.|Place on the onion in a slow cooker and pour over the sauce.|Cook on low for 8 hours, then shred.|Serve in buns with slaw.", False),
    ("Margherita Pizza", "Italian", 4, 3, "Homemade pizza night.",
     "2 pizza bases;1 cup passata;250 g mozzarella, torn;2 tomatoes, sliced;1 tbsp olive oil;1 tsp dried oregano;Fresh basil, to serve",
     "Heat the oven as hot as it goes.|Spread the bases with passata and scatter with oregano.|Top with tomato and mozzarella.|Bake for 10-12 minutes and finish with basil and oil.", False),
    ("Beef and Broccoli", "Chinese", 4, 2, "Takeaway-style beef and broccoli in 20 minutes.",
     "500 g beef rump, thinly sliced;2 heads broccoli, cut into florets;3 tbsp oyster sauce;2 tbsp soy sauce;1 tbsp cornflour;2 cloves garlic, crushed;1 tbsp fresh ginger, grated;2 cups jasmine rice",
     "Toss the beef with cornflour and soy.|Sear the beef in a very hot wok and remove.|Stir fry broccoli with garlic and ginger and a splash of water.|Return the beef with oyster sauce and toss.|Serve over rice.", False),
    ("Shepherd's Pie", "British", 6, 3, "Lamb mince and veg under fluffy mash.",
     "600 g lamb mince;1 onion, diced;2 carrots, diced;1 cup frozen peas;2 tbsp tomato paste;1 cup beef stock;1 tbsp worcestershire sauce;1 kg potatoes, peeled;50 g butter",
     "Brown the mince with onion and carrot.|Add paste, stock and worcestershire and simmer 20 minutes; stir in peas.|Boil and mash the potatoes with butter.|Top the mince with mash and bake at 200C for 25 minutes.", False),
    ("Honey Soy Chicken Drumsticks", "New Zealand", 4, 1, "Sticky drumsticks the kids demolish.",
     "8 chicken drumsticks;4 tbsp soy sauce;3 tbsp honey;2 cloves garlic, crushed;1 tbsp sesame oil;2 cups jasmine rice;2 cups frozen mixed vegetables",
     "Mix soy, honey, garlic and sesame oil and coat the drumsticks.|Bake at 200C for 40 minutes, basting halfway.|Serve with rice and vegetables.", False),
    ("Prawn Pad Thai", "Thai", 4, 3, "Sweet, sour and salty noodles with prawns.",
     "300 g prawns, peeled;250 g rice noodles;2 eggs, beaten;2 tbsp fish sauce;2 tbsp brown sugar;1 lime, juiced;2 cups bean sprouts;3 spring onions, sliced;0.25 cup roasted peanuts, chopped",
     "Soak the noodles until soft.|Stir fry prawns until pink, push aside and scramble the eggs.|Add noodles, fish sauce, sugar and lime and toss.|Fold through sprouts and spring onion and top with peanuts.", False),
    ("Chilli Con Carne", "Mexican", 6, 2, "Big batch chilli for rice, nachos or baked potatoes.",
     "500 g beef mince;1 onion, diced;1 red capsicum, diced;2 cans chopped tomatoes;1 can kidney beans, drained;2 tsp ground cumin;1 tsp smoked paprika;1 tsp chilli powder;2 cups basmati rice;Sour cream, to serve",
     "Brown mince with onion and capsicum.|Add spices and cook 1 minute.|Add tomatoes and beans and simmer 30 minutes.|Serve with rice and sour cream.", False),
    ("Creamy Chicken and Bacon Pasta", "Italian", 4, 2, "Comfort food in the time it takes to boil pasta.",
     "400 g penne;400 g chicken breast, diced;4 rashers bacon, chopped;250 g mushrooms, sliced;2 cloves garlic, crushed;250 ml cream;2 handfuls baby spinach;0.5 cup parmesan, grated",
     "Cook the pasta.|Fry bacon and chicken until golden, then add mushrooms and garlic.|Pour in cream and simmer until slightly thickened.|Toss through pasta, spinach and parmesan.", False),
    ("Pork Dumplings with Greens", "Chinese", 4, 1, "Frozen dumplings made into a meal.",
     "40 frozen pork dumplings;2 heads bok choy, halved;1 head broccoli, cut into florets;3 tbsp soy sauce;1 tbsp rice vinegar;1 tsp sesame oil;Chilli oil, to serve",
     "Steam or pan fry the dumplings according to the packet.|Steam the greens for 3 minutes.|Mix soy, vinegar and sesame oil for dipping.", False),
    ("Moroccan Lamb Tagine", "Moroccan", 6, 4, "Slow-cooked lamb with apricots and warm spices.",
     "800 g lamb shoulder, diced;2 onions, sliced;3 cloves garlic, crushed;2 tsp ground cumin;2 tsp ground coriander;1 tsp cinnamon;1 can chopped tomatoes;1 can chickpeas, drained;0.5 cup dried apricots;2 cups couscous",
     "Brown the lamb in batches.|Soften onion and garlic with the spices.|Add tomatoes, apricots and a cup of water; cover and simmer 1.5 hours.|Add chickpeas for the last 15 minutes.|Serve over couscous.", False),
    ("Bangers and Mash", "British", 4, 1, "Sausages, mash, peas, onion gravy.",
     "8 beef sausages;1 kg potatoes, peeled;50 g butter;100 ml milk;2 onions, sliced;2 tbsp gravy powder;2 cups frozen peas",
     "Boil and mash the potatoes with butter and milk.|Fry the sausages until cooked through.|Caramelise the onions and make gravy.|Serve with peas.", False),
    ("Chicken Caesar Salad", "American", 4, 2, "A salad that counts as dinner.",
     "500 g chicken breast;2 cos lettuces, torn;4 rashers bacon, chopped;4 eggs, soft boiled;2 slices sourdough, cubed;0.5 cup caesar dressing;0.5 cup parmesan, shaved",
     "Grill the chicken and slice.|Fry bacon and sourdough cubes until crisp.|Toss lettuce with dressing.|Top with chicken, bacon, croutons, egg and parmesan.", False),
    ("Hoisin Pork Noodles", "Chinese", 4, 2, "Quick pork mince noodles.",
     "500 g pork mince;400 g egg noodles;3 tbsp hoisin sauce;2 tbsp soy sauce;2 carrots, julienned;1 cup green beans, sliced;2 cloves garlic, crushed;3 spring onions, sliced",
     "Cook the noodles.|Brown the mince with garlic.|Add vegetables and stir fry 3 minutes.|Toss through noodles and sauces and top with spring onion.", False),
    ("Kumara and Chickpea Curry", "Indian", 4, 2, "Vegetarian curry with NZ kumara.",
     "600 g kumara, cubed;1 can chickpeas, drained;1 can coconut milk;1 onion, diced;2 tbsp curry powder;1 can chopped tomatoes;2 handfuls baby spinach;2 cups basmati rice;4 naan breads",
     "Soften the onion and stir in curry powder.|Add kumara, tomatoes and coconut milk and simmer 20 minutes.|Stir in chickpeas and spinach.|Serve with rice and naan.", False),
    ("Steak with Chimichurri", "Argentinian", 4, 3, "Seared steak, herby sauce, roast potatoes.",
     "4 sirloin steaks;1 bunch parsley, finely chopped;3 cloves garlic, crushed;3 tbsp red wine vinegar;0.5 cup olive oil;1 tsp chilli flakes;800 g potatoes, cubed;200 g green beans, trimmed",
     "Roast the potatoes at 210C for 35 minutes.|Mix parsley, garlic, vinegar, oil and chilli for the chimichurri.|Sear the steaks 3 minutes each side and rest.|Steam the beans and serve everything together.", False),
    ("Tuna Pasta Bake", "Italian", 6, 1, "Pantry dinner for the end of the week.",
     "500 g pasta shells;2 cans tuna, drained;1 can chopped tomatoes;1 cup corn kernels;1 onion, diced;1 cup cream;2 cups grated cheese",
     "Cook the pasta.|Soften the onion and add tomatoes and cream.|Stir through pasta, tuna and corn and tip into a dish.|Top with cheese and bake at 200C for 20 minutes.", False),
    ("Greek Chicken Souvlaki", "Greek", 4, 3, "Lemon-oregano chicken skewers with tzatziki.",
     "600 g chicken thighs, cubed;2 lemons, juiced;2 tsp dried oregano;3 cloves garlic, crushed;4 pita breads;1 cup greek yoghurt;1 cucumber, grated;2 tomatoes, diced;1 red onion, sliced",
     "Marinate the chicken in lemon, oregano and garlic.|Thread onto skewers and grill for 12 minutes.|Stir cucumber into the yoghurt.|Serve in warm pita with tomato and onion.", False),
    ("Bacon and Egg Pie", "New Zealand", 6, 2, "Picnic classic that works for dinner too.",
     "2 sheets puff pastry;8 rashers bacon, chopped;10 eggs;1 onion, finely diced;1 cup frozen peas;2 tomatoes, sliced;2 tbsp milk",
     "Line a dish with pastry.|Scatter over bacon, onion and peas and crack in the eggs.|Top with tomato and a pastry lid and brush with milk.|Bake at 200C for 40 minutes.", False),
    ("Miso Ramen", "Japanese", 4, 3, "Brothy noodle bowls with jammy eggs.",
     "4 packets ramen noodles;1 litre chicken stock;3 tbsp miso paste;300 g pork belly, sliced;4 eggs, soft boiled;2 heads bok choy, halved;1 cup corn kernels;3 spring onions, sliced",
     "Simmer the stock and whisk in the miso.|Pan fry the pork belly until crisp.|Cook noodles and bok choy in the broth.|Top bowls with pork, egg, corn and spring onion.", False),
    ("Nachos", "Mexican", 4, 1, "Friday night nachos.",
     "400 g beef mince;1 can chilli beans;1 can chopped tomatoes;200 g corn chips;2 cups grated cheese;1 avocado, mashed;Sour cream, to serve",
     "Brown the mince and simmer with beans and tomatoes for 15 minutes.|Pile onto corn chips and cover with cheese.|Grill until bubbling.|Top with avocado and sour cream.", False),
    # --- 6 dinner recipes with no effort-N tag ("unrated") ---
    ("Chicken Laksa", "Malaysian", 4, None, "Spicy coconut noodle soup.",
     "500 g chicken thighs, sliced;3 tbsp laksa paste;1 can coconut milk;500 ml chicken stock;250 g rice noodles;2 cups bean sprouts;1 lime, cut into wedges;Fresh coriander, to serve",
     "Fry the paste until fragrant.|Add stock and coconut milk and bring to a simmer.|Poach the chicken in the broth for 10 minutes.|Serve over noodles with sprouts, lime and coriander.", False),
    ("Beef Rendang", "Indonesian", 6, None, "Rich, dry coconut beef curry.",
     "1 kg beef chuck, cubed;4 tbsp rendang paste;1 can coconut milk;2 stalks lemongrass, bruised;4 kaffir lime leaves;0.5 cup desiccated coconut, toasted;2 cups jasmine rice",
     "Brown the beef with the paste.|Add coconut milk, lemongrass and lime leaves.|Simmer uncovered for 2.5 hours until dark and almost dry.|Stir through toasted coconut and serve with rice.", True),
    ("Vegetable Lasagne", "Italian", 6, None, "Layers of roast vegetables, spinach and ricotta.",
     "9 lasagne sheets;2 zucchini, sliced;1 eggplant, sliced;2 red capsicums, sliced;500 g passata;250 g ricotta;200 g baby spinach;2 cups grated cheese",
     "Roast the vegetables at 200C for 20 minutes.|Wilt the spinach and mix with ricotta.|Layer passata, pasta, vegetables and ricotta, finishing with cheese.|Bake at 180C for 45 minutes.", True),
    ("Pork Chops with Apple Sauce", "British", 4, None, "Pan-fried chops with quick apple sauce and greens.",
     "4 pork chops;3 apples, peeled and diced;1 tbsp brown sugar;800 g potatoes, cubed;200 g green beans, trimmed;1 tbsp olive oil;1 tsp dijon mustard",
     "Simmer apples with sugar and a splash of water until soft.|Roast the potatoes.|Pan fry the chops 5 minutes each side.|Serve with beans and apple sauce.", True),
    ("Korean Beef Bowls", "Korean", 4, None, "Sweet-savoury mince on rice with quick pickles.",
     "500 g beef mince;4 tbsp soy sauce;2 tbsp brown sugar;1 tbsp sesame oil;3 cloves garlic, crushed;2 cups sushi rice;1 cucumber, thinly sliced;2 carrots, grated;4 eggs, fried",
     "Brown the mince with garlic.|Add soy, sugar and sesame oil and simmer until sticky.|Serve over rice with cucumber, carrot and a fried egg.", False),
    ("Smoked Fish Kedgeree", "British", 4, None, "Curried rice with smoked fish and egg.",
     "400 g smoked fish, flaked;2 cups basmati rice;1 onion, diced;1 tbsp curry powder;4 eggs, hard boiled;1 cup frozen peas;50 g butter;Chopped parsley, to serve",
     "Cook the rice.|Soften the onion in butter with curry powder.|Fold through rice, fish and peas.|Top with quartered eggs and parsley.", True),
    # --- rated dinners that still look like fresh, un-parsed imports ---
    ("Chicken Katsu Curry", "Japanese", 4, 4, "Crumbed chicken with Japanese curry sauce.",
     "4 chicken breasts, flattened;1 cup panko breadcrumbs;2 eggs, beaten;1 packet japanese curry roux;2 carrots, diced;2 potatoes, diced;1 onion, sliced;2 cups sushi rice",
     "Simmer onion, carrot and potato in 600ml water until tender, then dissolve in the roux.|Crumb the chicken and shallow fry until golden.|Slice the chicken and serve on rice with the curry sauce.", True),
    ("Falafel Wraps", "Middle Eastern", 4, 2, "Baked falafel with hummus and salad.",
     "2 cans chickpeas, drained;1 onion, roughly chopped;3 cloves garlic;1 bunch parsley;2 tsp ground cumin;3 tbsp flour;6 wraps;0.5 cup hummus;2 tomatoes, sliced;Shredded lettuce, to serve",
     "Blitz chickpeas, onion, garlic, parsley, cumin and flour to a coarse paste.|Shape into patties and bake at 200C for 25 minutes, turning once.|Serve in wraps with hummus and salad.", True),
    ("Venison Sausage Cassoulet", "French", 6, 3, "Hearty bean and sausage stew.",
     "8 venison sausages;2 cans cannellini beans, drained;1 can chopped tomatoes;2 carrots, diced;2 stalks celery, diced;1 onion, diced;1 cup chicken stock;1 tsp dried thyme",
     "Brown the sausages and set aside.|Soften the vegetables.|Add beans, tomatoes, stock, thyme and the sausages.|Simmer for 40 minutes until thick.", True),
    ("Sweet and Sour Pork", "Chinese", 4, 3, "Crispy pork with pineapple and capsicum.",
     "500 g pork loin, cubed;1 can pineapple pieces;1 red capsicum, cut into chunks;1 onion, cut into wedges;3 tbsp tomato sauce;2 tbsp rice vinegar;2 tbsp cornflour;2 cups jasmine rice",
     "Toss the pork in cornflour and fry until crisp.|Stir fry the capsicum and onion.|Add pineapple with its juice, tomato sauce and vinegar and bubble until glossy.|Return the pork and serve over rice.", True),
    ("Spinach and Ricotta Cannelloni", "Italian", 4, 3, "Stuffed pasta tubes baked in tomato sauce.",
     "250 g cannelloni tubes;500 g ricotta;200 g baby spinach, wilted;1 egg;700 ml passata;2 cloves garlic, crushed;1 cup grated cheese;Fresh basil, to serve",
     "Mix ricotta, spinach and egg and pipe into the tubes.|Lay in a dish and cover with passata and garlic.|Top with cheese.|Bake covered at 180C for 30 minutes, then 10 uncovered.", True),
    # --- non-dinner recipes (no dinner tag) ---
    ("Overnight Oats", "Breakfast", 2, None, "Make-ahead breakfast jars.",
     "1 cup rolled oats;1 cup milk;0.5 cup greek yoghurt;1 tbsp chia seeds;1 tbsp honey;1 cup frozen berries",
     "Stir everything except the berries together in a jar.|Refrigerate overnight.|Top with berries to serve.", False),
    ("Anzac Biscuits", "Baking", 24, None, "Chewy golden syrup and oat biscuits.",
     "1 cup rolled oats;1 cup flour;1 cup desiccated coconut;0.75 cup brown sugar;125 g butter;2 tbsp golden syrup;1 tsp baking soda",
     "Mix the dry ingredients.|Melt butter and golden syrup, then stir in baking soda dissolved in 2 tbsp boiling water.|Combine and roll into balls on a lined tray.|Bake at 170C for 12-15 minutes.", False),
    ("Banana Bread", "Baking", 10, None, "The thing to do with brown bananas.",
     "3 bananas, mashed;125 g butter, melted;0.75 cup brown sugar;2 eggs;1.5 cups flour;1 tsp baking soda;1 tsp cinnamon",
     "Whisk butter, sugar, eggs and banana.|Fold in flour, baking soda and cinnamon.|Pour into a lined loaf tin.|Bake at 170C for 55 minutes.", False),
    ("Cheese Scones", "Baking", 8, None, "Lunchbox favourite.",
     "3 cups self-raising flour;2 cups grated cheese;1 cup milk;50 g butter, cold;1 pinch salt;1 pinch cayenne pepper",
     "Rub butter into flour, salt and cayenne.|Stir in the cheese, then the milk, until just combined.|Pat out and cut into 8.|Bake at 220C for 12-15 minutes.", False),
    ("Pikelets", "Baking", 4, None, "Quick afternoon tea.",
     "1 cup self-raising flour;1 egg;0.75 cup milk;2 tbsp sugar;1 tbsp butter, melted;Jam and cream, to serve",
     "Whisk everything to a smooth batter.|Drop spoonfuls into a greased pan over medium heat.|Flip when bubbles appear and cook until golden.", False),
    ("Berry Smoothie", "Breakfast", 2, None, "Two-minute breakfast.",
     "2 cups frozen berries;1 banana;1 cup milk;0.5 cup greek yoghurt;1 tbsp honey;1 tbsp rolled oats",
     "Blend everything until smooth.|Add more milk if too thick.|Pour and serve straight away.", True),
]
NON_DINNER = {"Overnight Oats", "Anzac Biscuits", "Banana Bread", "Cheese Scones", "Pikelets", "Berry Smoothie"}
NO_IMAGE = {"Pikelets", "Venison Sausage Cassoulet", "Smoked Fish Kedgeree"}  # image: null
VARIANT_PREFIXES = ["Quick", "Slow-Cooker", "Spicy", "One-Pan", "Nana's", "Weeknight", "Smoky", "Creamy",
                    "Easy", "Mum's", "Cheat's", "Leftover", "Winter", "Summer", "Budget", "Fancy"]

# canonical unit name -> (abbreviation, [aliases])
UNIT_DEFS: dict[str, tuple[str, list[str]]] = {
    "gram": ("g", ["g", "grams", "gm", "gms"]),
    "kilogram": ("kg", ["kg", "kgs", "kilograms", "kilo"]),
    "millilitre": ("ml", ["ml", "mls", "milliliter", "milliliters", "millilitres"]),
    "litre": ("l", ["l", "liter", "liters", "litres"]),
    "teaspoon": ("tsp", ["tsp", "tsps", "teaspoons"]),
    "tablespoon": ("tbsp", ["tbsp", "tbsps", "tbs", "tablespoons"]),
    "cup": ("", ["cups"]),
    "can": ("", ["cans", "tin", "tins"]),
    "clove": ("", ["cloves"]),
    "pinch": ("", ["pinches"]),
    "bunch": ("", ["bunches"]),
    "packet": ("pkt", ["pkt", "packets", "pack", "packs"]),
    "slice": ("", ["slices"]),
    "sprig": ("", ["sprigs"]),
    "handful": ("", ["handfuls"]),
    "sheet": ("", ["sheets"]),
    "rasher": ("", ["rashers"]),
    "head": ("", ["heads"]),
    "stalk": ("", ["stalks"]),
    "piece": ("", ["pieces"]),
}
UNIT_LOOKUP = {a: name for name, (_, aliases) in UNIT_DEFS.items() for a in [name, *aliases]}
FRACTIONS = {"½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4", "⅛": "1/8"}
QTY_RE = re.compile(
    r"^\s*(\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"         # 1 1/2 | 1/2 | 1.5 | 2
    r"(?:\s*(?:-|–|to)\s*\d+(?:\.\d+)?(?:/\d+)?)?\s*"    # optional range "2-3": first number wins
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def slugify(text: str) -> str:
    text = text.lower().replace("'", "").replace("’", "")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-") or "recipe"


def ts(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def fmt_qty(q: float) -> str:
    return f"{q:g}" if q else ""


def paginate(request: Request, items: list, default_per_page: int = 50) -> dict:
    """Mealie's pagination envelope, honouring ?page= & ?perPage= (-1 = everything)."""
    qp = request.query_params
    try:
        page = max(1, int(qp.get("page", 1)))
        per_page = int(qp.get("perPage", default_per_page))
    except ValueError:
        raise HTTPException(422, "page and perPage must be integers")
    total = len(items)
    if per_page == -1:
        per_page, page = max(total, 1), 1
    per_page = max(1, per_page)
    total_pages = max(1, math.ceil(total / per_page))
    start = (page - 1) * per_page

    def link(p: int) -> Optional[str]:
        if p < 1 or p > total_pages:
            return None
        params = {k: v for k, v in qp.multi_items() if k not in ("page", "perPage")}
        return f"{request.url.path.removeprefix('/api')}?{urlencode({**params, 'page': p, 'perPage': per_page})}"

    return {
        "page": page,
        "per_page": per_page,
        "total": total,
        "total_pages": total_pages,
        "items": items[start:start + per_page],
        "next": link(page + 1),
        "previous": link(page - 1),
    }


def order(request: Request, items: list, default_key: Optional[str] = None) -> list:
    qp = request.query_params
    key = qp.get("orderBy") or default_key
    if not key:
        return items
    if key == "random":
        out = list(items)
        random.Random(qp.get("paginationSeed", "0")).shuffle(out)
        return out
    key = re.sub(r"_([a-z])", lambda m: m.group(1).upper(), key)  # created_at -> createdAt
    reverse = (qp.get("orderDirection") or "asc").lower() == "desc"
    return sorted(items, key=lambda x: (x.get(key) is None, str(x.get(key) or "").lower()), reverse=reverse)


def bad_request(message: str, exception: str = "") -> HTTPException:
    return HTTPException(400, {"message": message, "error": True, "exception": exception or None})


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #
class State:
    def __init__(self, n_recipes: Optional[int] = None):
        self.n_recipes = n_recipes
        self.reset()

    # ---- ids ----
    def new_id(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def reset(self) -> None:
        self.rng = random.Random(20240914)  # every id / choice below derives from this
        self.today = date.today()
        self.recipes: dict[str, dict] = {}          # id -> full recipe
        self.images: dict[str, tuple[bytes, str]] = {}  # recipe id -> (bytes, content-type)
        self.tags: dict[str, dict] = {}
        self.foods: dict[str, dict] = {}
        self.units: dict[str, dict] = {}
        self.mealplans: dict[int, dict] = {}
        self.next_mealplan_id = 1
        self.lists: dict[str, dict] = {}
        self.items: dict[str, dict] = {}
        self._seed()

    # ---- organizers ----
    def tag_by(self, value: str) -> Optional[dict]:
        v = (value or "").lower()
        for t in self.tags.values():
            if v in (t["id"], t["slug"], t["name"].lower()):
                return t
        return None

    def create_tag(self, name: str) -> dict:
        tag = {"id": self.new_id(), "groupId": GROUP_ID, "name": name, "slug": slugify(name)}
        self.tags[tag["id"]] = tag
        return tag

    def food_by_name(self, name: str) -> Optional[dict]:
        n = (name or "").strip().lower()
        if not n:
            return None
        candidates = {n, n.rstrip("s"), n[:-2] if n.endswith("es") else n}
        for f in self.foods.values():
            names = {f["name"].lower(), (f.get("pluralName") or "").lower(),
                     *(a["name"].lower() for a in f.get("aliases") or [])}
            if names & candidates:
                return f
        return None

    def create_food(self, body: dict) -> dict:
        now = ts()
        food = {
            "id": self.new_id(),
            "name": body["name"].strip(),
            "pluralName": body.get("pluralName"),
            "description": body.get("description") or "",
            "extras": body.get("extras") or {},
            "labelId": body.get("labelId"),
            "aliases": body.get("aliases") or [],
            "householdsWithIngredientFood": [],
            "label": None,
            "createdAt": now,
            "updatedAt": now,
        }
        self.foods[food["id"]] = food
        return food

    def unit_by_name(self, name: str) -> Optional[dict]:
        n = (name or "").strip().lower().rstrip(".")
        if not n:
            return None
        canonical = UNIT_LOOKUP.get(n, n)
        for u in self.units.values():
            names = {u["name"].lower(), (u.get("pluralName") or "").lower(), (u.get("abbreviation") or "").lower(),
                     (u.get("pluralAbbreviation") or "").lower(), *(a["name"].lower() for a in u.get("aliases") or [])}
            names.discard("")
            if n in names or canonical in names:
                return u
        return None

    def create_unit(self, body: dict) -> dict:
        now = ts()
        unit = {
            "id": self.new_id(),
            "name": body["name"].strip(),
            "pluralName": body.get("pluralName"),
            "description": body.get("description") or "",
            "extras": body.get("extras") or {},
            "fraction": body.get("fraction", True),
            "abbreviation": body.get("abbreviation") or "",
            "pluralAbbreviation": body.get("pluralAbbreviation") or "",
            "useAbbreviation": body.get("useAbbreviation", False),
            "aliases": body.get("aliases") or [],
            "createdAt": now,
            "updatedAt": now,
        }
        self.units[unit["id"]] = unit
        return unit

    # ---- ingredient parsing ----
    def parse_ingredient(self, text: str, create: bool = False) -> dict:
        """Regex stand-in for Mealie's NLP parser. Returns a recipeIngredient dict.

        Foods/units already in the database come back whole (with `id`); unknown
        ones come back as `{"id": None, "name": ...}` unless `create` is set.
        """
        original = (text or "").strip()
        s = original
        for k, v in FRACTIONS.items():
            s = s.replace(k, f" {v}")
        quantity, unit, unit_name = 0.0, None, None
        m = QTY_RE.match(s)
        if m:
            raw = m.group(1)
            if " " in raw:
                whole, frac = raw.split()
                num, den = frac.split("/")
                quantity = float(whole) + float(num) / (float(den) or 1)
            elif "/" in raw:
                num, den = raw.split("/")
                quantity = float(num) / (float(den) or 1)
            else:
                quantity = float(raw)
            s = s[m.end():]
            um = re.match(r"([A-Za-z]+)\.?(?=\s|$)\s*(?:of\s+)?", s)
            if um and um.group(1).lower() in UNIT_LOOKUP:
                unit_name = UNIT_LOOKUP[um.group(1).lower()]
                s = s[um.end():]
        notes = re.findall(r"\(([^)]*)\)", s)
        s = re.sub(r"\s*\([^)]*\)", "", s)
        food_name, _, after = s.partition(",")
        food_name = food_name.strip()
        notes = [n.strip() for n in [after, *notes] if n.strip()]
        note = ", ".join(notes)

        if unit_name:
            unit = self.unit_by_name(unit_name)
            if unit is None:
                unit = (self.create_unit({"name": unit_name, "abbreviation": UNIT_DEFS[unit_name][0],
                                          "useAbbreviation": bool(UNIT_DEFS[unit_name][0])})
                        if create else {"id": None, "name": unit_name})
        food = None
        if food_name:
            food = self.food_by_name(food_name)
            if food is None:
                food = self.create_food({"name": food_name}) if create else {"id": None, "name": food_name}
        ing = {"quantity": round(quantity, 3), "unit": unit, "food": food, "note": note,
               "title": None, "originalText": original, "referenceId": str(uuid.uuid4())}
        ing["display"] = display_ingredient(ing)
        return ing

    def note_only_ingredient(self, text: str) -> dict:
        ing = {"quantity": 0.0, "unit": None, "food": None, "note": text.strip(), "title": None,
               "originalText": text.strip(), "referenceId": self.new_id()}
        ing["display"] = display_ingredient(ing)
        return ing

    def normalise_ingredient(self, raw: Any) -> dict:
        """Validate an ingredient coming in on PUT /recipes the way real Mealie does:
        food/unit objects must reference rows that already exist."""
        if isinstance(raw, str):
            return self.note_only_ingredient(raw)
        ing = {
            "quantity": float(raw.get("quantity") or 0),
            "unit": None,
            "food": None,
            "note": raw.get("note") or "",
            "title": raw.get("title"),
            "originalText": raw.get("originalText"),
            "referenceId": raw.get("referenceId") or str(uuid.uuid4()),
        }
        for kind, store in (("unit", self.units), ("food", self.foods)):
            obj = raw.get(kind)
            if not obj:
                continue
            if not isinstance(obj, dict) or not obj.get("id") or obj["id"] not in store:
                name = obj.get("name") if isinstance(obj, dict) else obj
                raise bad_request(
                    f"Recipe ingredient references a {kind} that does not exist: {name!r}. "
                    f"Create it via POST /api/{kind}s first.",
                    "ForeignKeyViolation",
                )
            ing[kind] = store[obj["id"]]
        ing["display"] = display_ingredient(ing)
        return ing

    # ---- recipes ----
    def unique_slug(self, name: str, ignore_id: Optional[str] = None) -> str:
        base = slugify(name)
        taken = {r["slug"] for r in self.recipes.values() if r["id"] != ignore_id}
        slug, n = base, 0
        while slug in taken:
            n += 1
            slug = f"{base}-{n}"
        return slug

    def recipe_by_slug(self, slug_or_id: str) -> dict:
        if slug_or_id in self.recipes:
            return self.recipes[slug_or_id]
        for r in self.recipes.values():
            if r["slug"] == slug_or_id:
                return r
        raise HTTPException(404, {"message": "Recipe not found", "error": True, "exception": None})

    def create_recipe(self, name: str, *, description: str = "", servings: float = 0, ingredients: list = (),
                      steps: list = (), tags: list = (), org_url: Optional[str] = None, image: Optional[str] = None,
                      created: Optional[datetime] = None, cuisine: str = "") -> dict:
        created = created or datetime.now(timezone.utc)
        rid = self.new_id()
        recipe = {
            "id": rid,
            "userId": USER_ID,
            "householdId": HOUSEHOLD_ID,
            "groupId": GROUP_ID,
            "name": name,
            "slug": self.unique_slug(name),
            "image": image,
            "recipeServings": float(servings or 0),
            "recipeYieldQuantity": 0.0,
            "recipeYield": None,
            "totalTime": None,
            "prepTime": None,
            "cookTime": None,
            "performTime": None,
            "description": description,
            "recipeCategory": [],
            "tags": [dict(t) for t in tags],
            "tools": [],
            "rating": None,
            "orgURL": org_url,
            "dateAdded": created.date().isoformat(),
            "dateUpdated": ts(created),
            "createdAt": ts(created),
            "updatedAt": ts(created),
            "lastMade": None,
            # ---- full-recipe-only fields ----
            "recipeIngredient": list(ingredients),
            "recipeInstructions": [
                {"id": self.new_id(), "title": "", "summary": "", "text": s, "ingredientReferences": []}
                for s in steps
            ],
            "nutrition": {"calories": None, "carbohydrateContent": None, "cholesterolContent": None,
                          "fatContent": None, "fiberContent": None, "proteinContent": None,
                          "saturatedFatContent": None, "sodiumContent": None, "sugarContent": None,
                          "transFatContent": None, "unsaturatedFatContent": None},
            "settings": {"public": False, "showNutrition": False, "showAssets": False, "landscapeView": False,
                         "disableComments": False, "locked": False},
            "assets": [],
            "notes": [],
            "extras": {"cuisine": cuisine} if cuisine else {},
            "comments": [],
        }
        self.recipes[rid] = recipe
        return recipe

    SUMMARY_DROP = ("recipeIngredient", "recipeInstructions", "nutrition", "settings", "assets", "notes",
                    "extras", "comments")

    def summary(self, recipe: dict) -> dict:
        return {k: v for k, v in recipe.items() if k not in self.SUMMARY_DROP}

    # ---- meal plans ----
    def add_mealplan(self, day: date, entry_type: str = "dinner", recipe_id: Optional[str] = None,
                     title: str = "", text: str = "") -> dict:
        entry = {
            "date": day.isoformat(),
            "entryType": entry_type,
            "title": title or "",
            "text": text or "",
            "recipeId": recipe_id,
            "id": self.next_mealplan_id,
            "groupId": GROUP_ID,
            "userId": USER_ID,
            "householdId": HOUSEHOLD_ID,
        }
        self.mealplans[entry["id"]] = entry
        self.next_mealplan_id += 1
        return entry

    def mealplan_out(self, entry: dict) -> dict:
        recipe = self.recipes.get(entry["recipeId"]) if entry["recipeId"] else None
        return {**entry, "recipe": self.summary(recipe) if recipe else None}

    # ---- shopping ----
    def create_list(self, name: str) -> dict:
        now = ts()
        sl = {"id": self.new_id(), "name": name, "extras": {}, "createdAt": now, "updatedAt": now,
              "groupId": GROUP_ID, "userId": USER_ID, "householdId": HOUSEHOLD_ID,
              "recipeReferences": [], "labelSettings": []}
        self.lists[sl["id"]] = sl
        return sl

    def list_items(self, list_id: str) -> list[dict]:
        return sorted((i for i in self.items.values() if i["shoppingListId"] == list_id),
                      key=lambda i: (i["checked"], i["position"]))

    def list_out(self, sl: dict, full: bool = True) -> dict:
        out = {**sl, "recipeReferences": [
            {**ref, "recipe": self.summary(self.recipes[ref["recipeId"]]) if ref["recipeId"] in self.recipes else None}
            for ref in sl["recipeReferences"]]}
        if full:
            out["listItems"] = self.list_items(sl["id"])
        return out

    def add_item(self, list_id: str, *, quantity: float = 1.0, unit: Optional[dict] = None,
                 food: Optional[dict] = None, note: str = "", checked: bool = False,
                 recipe_ref: Optional[dict] = None) -> tuple[dict, bool]:
        """Add an item, merging into an existing unchecked item with the same food+unit
        (or, for food-less items, the same note). Returns (item, created?)."""
        food_id, unit_id = (food or {}).get("id"), (unit or {}).get("id")
        now = ts()
        for item in self.list_items(list_id):
            if item["checked"] or checked:
                continue
            same = (item["foodId"] == food_id and item["unitId"] == unit_id) if food_id else (
                not item["foodId"] and item["note"].strip().lower() == note.strip().lower())
            if not same:
                continue
            item["quantity"] = round(item["quantity"] + quantity, 3)
            if food_id and note and note.lower() not in item["note"].lower():
                item["note"] = f"{item['note']} | {note}" if item["note"] else note
            if recipe_ref:
                for ref in item["recipeReferences"]:
                    if ref["recipeId"] == recipe_ref["recipeId"]:
                        ref["recipeQuantity"] += recipe_ref["recipeQuantity"]
                        break
                else:
                    item["recipeReferences"].append({**recipe_ref, "id": self.new_id(),
                                                     "shoppingListItemId": item["id"]})
            item["display"] = display_ingredient(item)
            item["updatedAt"] = now
            return item, False
        iid = self.new_id()
        item = {
            "id": iid, "shoppingListId": list_id, "quantity": round(float(quantity), 3), "unit": unit, "food": food,
            "note": note or "", "checked": bool(checked),
            "position": len([i for i in self.items.values() if i["shoppingListId"] == list_id]),
            "foodId": food_id, "unitId": unit_id, "labelId": None, "label": None, "extras": {},
            "groupId": GROUP_ID, "householdId": HOUSEHOLD_ID,
            "recipeReferences": [{**recipe_ref, "id": self.new_id(), "shoppingListItemId": iid}] if recipe_ref else [],
            "createdAt": now, "updatedAt": now,
        }
        item["display"] = display_ingredient(item)
        self.items[iid] = item
        return item, True

    def add_recipe_to_list(self, sl: dict, recipe: dict, scale: float) -> tuple[list, list]:
        created, updated = [], []
        for ing in recipe["recipeIngredient"]:
            if not ing.get("food") and not (ing.get("note") or "").strip():
                continue
            item, was_created = self.add_item(
                sl["id"], quantity=(ing.get("quantity") or 0) * scale, unit=ing.get("unit"), food=ing.get("food"),
                note=ing.get("note") or "",
                recipe_ref={"recipeId": recipe["id"], "recipeQuantity": scale, "recipeScale": scale,
                            "recipeNote": ing.get("note") or None},
            )
            (created if was_created else updated).append(item)
        for ref in sl["recipeReferences"]:
            if ref["recipeId"] == recipe["id"]:
                ref["recipeQuantity"] += scale
                break
        else:
            sl["recipeReferences"].append({"id": self.new_id(), "shoppingListId": sl["id"],
                                           "recipeId": recipe["id"], "recipeQuantity": scale})
        sl["updatedAt"] = ts()
        return created, updated

    # ---- seed ----
    def _seed(self) -> None:
        rng = self.rng
        now = datetime.combine(self.today, datetime.min.time(), tzinfo=timezone.utc).replace(hour=9)
        dinner = self.create_tag("dinner")
        effort = {n: self.create_tag(f"effort-{n}") for n in range(1, 6)}
        extra = {name: self.create_tag(name) for name in ("kid-friendly", "vegetarian", "freezer", "baking")}
        vegetarian = {"Pumpkin and Feta Risotto", "Vegetable Fried Rice", "Margherita Pizza", "Kumara and Chickpea Curry",
                      "Vegetable Lasagne", "Falafel Wraps", "Spinach and Ricotta Cannelloni"}
        for name, (abbr, _) in list(UNIT_DEFS.items())[:8]:  # gram ... can; the rest get created on demand
            self.create_unit({"name": name, "abbreviation": abbr, "useAbbreviation": bool(abbr)})

        base = list(SEED_RECIPES)
        n = self.n_recipes or len(base)
        specs = []
        for i in range(n):
            spec = base[i % len(base)]
            round_no = i // len(base)
            if round_no:
                prefix = VARIANT_PREFIXES[(round_no - 1) % len(VARIANT_PREFIXES)]
                suffix = f" {(round_no - 1) // len(VARIANT_PREFIXES) + 1}" if round_no > len(VARIANT_PREFIXES) else ""
                spec = (f"{prefix} {spec[0]}{suffix}", *spec[1:])
            specs.append((spec, base[i % len(base)][0]))

        for i, (spec, base_name) in enumerate(specs):
            name, cuisine, servings, eff, desc, ings, steps, raw = spec
            tags = []
            if base_name not in NON_DINNER:
                tags.append(dinner)
                if eff:
                    tags.append(effort[eff])
                if eff and eff <= 2 and rng.random() < 0.5:
                    tags.append(extra["kid-friendly"])
                if base_name in vegetarian:
                    tags.append(extra["vegetarian"])
            elif cuisine == "Baking":
                tags.append(extra["baking"])
            ingredients = []
            for line in ings.split(";"):
                if raw or not line[0].isdigit():
                    ingredients.append(self.note_only_ingredient(line))
                else:
                    ing = self.parse_ingredient(line, create=True)
                    ing["referenceId"] = self.new_id()
                    ingredients.append(ing)
            created = now - timedelta(days=rng.randint(30, 400), minutes=rng.randint(0, 600))
            self.create_recipe(
                name, description=desc, servings=servings, ingredients=ingredients, steps=steps.split("|"),
                tags=tags, cuisine=cuisine, created=created,
                org_url=f"https://recipes.example.com/{slugify(name)}" if rng.random() < 0.6 else None,
                image=None if base_name in NO_IMAGE else "".join(rng.choices("abcdefghijkmnpqrstuvwxyz23456789", k=4)),
            )
        # Insertion order would be suspiciously neat; real libraries are not alphabetical.
        ordered = list(self.recipes.values())
        rng.shuffle(ordered)
        self.recipes = {r["id"]: r for r in ordered}

        # Meal plans: a dinner every day for the last 3 weeks, plus 3 in the coming plan week.
        dinners = [r for r in self.recipes.values() if any(t["name"] == "dinner" for t in r["tags"])]
        picks = rng.sample(dinners, min(len(dinners), 24))

        def pick(i: int) -> Optional[str]:
            return picks[i % len(picks)]["id"] if picks else None

        for k, offset in enumerate(range(21, 0, -1)):
            day = self.today - timedelta(days=offset)
            if offset in (16, 9):  # a couple of recipe-less entries
                self.add_mealplan(day, "dinner", title="Takeaways" if offset == 16 else "Leftovers",
                                  text="Fish and chip shop" if offset == 16 else "")
            else:
                self.add_mealplan(day, "dinner", pick(k))
            if offset in (13, 6):
                self.add_mealplan(day, "lunch", title="Sandwiches")
        # The planner works in Sat->Wed blocks, so put the "already planned" entries on the
        # Saturday on/after today, then the Monday and Wednesday after it.
        saturday = self.today + timedelta(days=(5 - self.today.weekday()) % 7)
        for k, offset in enumerate((0, 2, 4)):
            self.add_mealplan(saturday + timedelta(days=offset), "dinner", pick(21 + k))

        # One existing shopping list.
        sl = self.create_list("Weekly shop")
        structured = [r for r in dinners if all(i["food"] for i in r["recipeIngredient"][:3])]
        for recipe in structured[:2]:
            self.add_recipe_to_list(sl, recipe, 1.0)
        for note in ("Dishwasher tablets", "Lunchbox snacks", "Toilet paper"):
            self.add_item(sl["id"], note=note)
        milk = self.food_by_name("milk")
        item, _ = self.add_item(sl["id"], quantity=2, unit=self.unit_by_name("litre"), food=milk, checked=True)
        stamp = ts(now - timedelta(days=2))
        sl["createdAt"] = sl["updatedAt"] = stamp
        for it in self.items.values():
            it["createdAt"] = it["updatedAt"] = stamp


def display_ingredient(ing: dict) -> str:
    unit = ing.get("unit") or {}
    unit_txt = (unit.get("abbreviation") if unit.get("useAbbreviation") else None) or unit.get("name") or ""
    parts = [fmt_qty(ing.get("quantity") or 0), unit_txt, (ing.get("food") or {}).get("name") or ""]
    main = " ".join(p for p in parts if p)
    note = ing.get("note") or ""
    if not (ing.get("food") or {}).get("name"):
        return " ".join(p for p in (main, note) if p)
    return f"{main}, {note}" if note else main


# --------------------------------------------------------------------------- #
# App
# --------------------------------------------------------------------------- #
STATE: State = None  # type: ignore[assignment]


def require_auth(request: Request) -> None:
    header = request.headers.get("authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(401, "Could not validate credentials", headers={"WWW-Authenticate": "Bearer"})


async def json_body(request: Request) -> Any:
    try:
        return await request.json()
    except Exception:
        raise HTTPException(422, "Request body must be valid JSON")


app = FastAPI(title="Fake Mealie", version=VERSION, docs_url="/__docs", redoc_url=None, openapi_url="/__openapi.json")
api = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


@app.exception_handler(HTTPException)
async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)


# ---- dev-only + unauthenticated endpoints ---------------------------------- #
@app.post("/__reset")
async def reset() -> dict:
    STATE.reset()
    return {"message": "state reset", "recipes": len(STATE.recipes), "mealplans": len(STATE.mealplans),
            "shoppingLists": len(STATE.lists)}


@app.get("/api/app/about")
async def about() -> dict:
    return {"production": False, "version": VERSION, "demoStatus": False, "allowSignup": False,
            "defaultGroupSlug": "home", "defaultHouseholdSlug": "family", "enableOidc": False,
            "enableOpenai": False, "enableOpenaiImageServices": False}


@app.post("/api/auth/token")
async def token(request: Request) -> dict:
    form = await request.form()
    if not form.get("username") or not form.get("password"):
        raise HTTPException(401, "Unauthorized")
    return {"access_token": "fake-" + hashlib.sha1(str(form["username"]).encode()).hexdigest()[:24],
            "token_type": "bearer"}


def placeholder_svg(recipe: dict, size: tuple[int, int]) -> bytes:
    w, h = size
    digest = hashlib.md5(recipe["id"].encode()).digest()
    hue = digest[0] / 255
    c1 = "#%02x%02x%02x" % tuple(int(c * 255) for c in colorsys.hls_to_rgb(hue, 0.52, 0.45))
    c2 = "#%02x%02x%02x" % tuple(int(c * 255) for c in colorsys.hls_to_rgb((hue + 0.08) % 1, 0.36, 0.5))
    initial = html.escape((recipe["name"].strip()[:1] or "?").upper())
    label = html.escape(recipe["name"])
    caption = (f'<text x="50%" y="86%" font-size="{h * 0.07:.0f}" fill="#fff" fill-opacity=".85" '
               f'text-anchor="middle" font-family="Helvetica,Arial,sans-serif">{label}</text>') if w >= 200 else ""
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
           f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{c1}"/>'
           f'<stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
           f'<rect width="{w}" height="{h}" fill="url(#g)"/>'
           f'<circle cx="{w / 2}" cy="{h * 0.45}" r="{h * 0.28}" fill="#fff" fill-opacity=".16"/>'
           f'<text x="50%" y="{h * 0.45}" dy=".35em" font-size="{h * 0.36:.0f}" font-weight="700" fill="#fff" '
           f'text-anchor="middle" font-family="Helvetica,Arial,sans-serif">{initial}</text>{caption}</svg>')
    return svg.encode()


IMAGE_SIZES = {"original.webp": (800, 600), "min-original.webp": (400, 300), "tiny-original.webp": (96, 96)}


@app.get("/api/media/recipes/{recipe_id}/images/{file_name}")
async def recipe_image(recipe_id: str, file_name: str) -> Response:
    recipe = STATE.recipes.get(recipe_id)
    if recipe is None or file_name not in IMAGE_SIZES:
        raise HTTPException(404, "Not found")
    if recipe_id in STATE.images:
        content, content_type = STATE.images[recipe_id]
        return Response(content, media_type=content_type, headers={"Cache-Control": "no-cache"})
    return Response(placeholder_svg(recipe, IMAGE_SIZES[file_name]), media_type="image/svg+xml",
                    headers={"Cache-Control": "no-cache"})


# ---- users ----------------------------------------------------------------- #
@api.get("/users/self")
async def users_self() -> dict:
    return {"id": USER_ID, "username": "dev", "fullName": "Dev User", "email": "dev@example.com",
            "authMethod": "Mealie", "admin": True, "group": "Home", "household": "Family",
            "groupId": GROUP_ID, "groupSlug": "home", "householdId": HOUSEHOLD_ID, "householdSlug": "family",
            "advanced": True, "canInvite": True, "canManage": True, "canOrganize": True, "cacheKey": "fake"}


# ---- recipes --------------------------------------------------------------- #
@api.get("/recipes")
async def list_recipes(request: Request) -> dict:
    qp = request.query_params
    recipes = list(STATE.recipes.values())
    wanted = [t for t in qp.getlist("tags") if t]
    if wanted:
        ids = {(STATE.tag_by(t) or {}).get("id", f"missing:{t}") for t in wanted}
        require_all = qp.get("requireAllTags", "false").lower() == "true"
        match = (lambda have: ids <= have) if require_all else (lambda have: bool(ids & have))
        recipes = [r for r in recipes if match({t["id"] for t in r["tags"]})]
    search = (qp.get("search") or "").strip().lower()
    if search:
        tokens = search.split()

        def haystack(r: dict) -> str:
            ings = " ".join(f"{(i.get('food') or {}).get('name') or ''} {i.get('note') or ''}"
                            for i in r["recipeIngredient"])
            return f"{r['name']} {r['description']} {ings}".lower()

        recipes = [r for r in recipes if all(tok in haystack(r) for tok in tokens)]
        recipes.sort(key=lambda r: (not all(tok in r["name"].lower() for tok in tokens), r["name"].lower()))
    recipes = order(request, recipes)
    return paginate(request, [STATE.summary(r) for r in recipes])


@api.post("/recipes", status_code=201)
async def create_recipe(request: Request) -> str:
    body = await json_body(request)
    name = (body.get("name") or "").strip() if isinstance(body, dict) else ""
    if not name:
        raise HTTPException(422, "name is required")
    recipe = STATE.create_recipe(name, ingredients=[STATE.note_only_ingredient("1 Cup Flour")],
                                 steps=["Recipe steps as well as other fields in the recipe page support markdown syntax."])
    return recipe["slug"]


def _schema_text(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("name") or "")
    return str(value or "")


def import_schema_recipe(data: dict, include_tags: bool = False, url: Optional[str] = None) -> dict:
    name = _schema_text(data.get("name")).strip()
    if not name:
        raise bad_request("Recipe data has no name", "ValueError")
    servings = 0.0
    yield_raw = data.get("recipeYield")
    if isinstance(yield_raw, list):
        yield_raw = yield_raw[0] if yield_raw else None
    m = re.search(r"\d+(?:\.\d+)?", str(yield_raw or ""))
    if m:
        servings = float(m.group())
    steps: list[str] = []
    instructions = data.get("recipeInstructions") or []
    if isinstance(instructions, str):
        instructions = [s for s in re.split(r"\n+", instructions) if s.strip()]
    for step in instructions:
        if isinstance(step, dict) and step.get("itemListElement"):
            steps.extend(_schema_text(s) for s in step["itemListElement"])
        else:
            steps.append(_schema_text(step))
    tags = []
    if include_tags:
        keywords = data.get("keywords") or []
        if isinstance(keywords, str):
            keywords = keywords.split(",")
        for kw in keywords:
            kw = str(kw).strip()
            if kw:
                tags.append(STATE.tag_by(kw) or STATE.create_tag(kw))
    cuisine = data.get("recipeCuisine") or ""
    if isinstance(cuisine, list):
        cuisine = ", ".join(map(str, cuisine))
    recipe = STATE.create_recipe(
        name,
        description=_schema_text(data.get("description")),
        servings=servings,
        ingredients=[STATE.note_only_ingredient(_schema_text(i)) for i in data.get("recipeIngredient") or []
                     if _schema_text(i).strip()],
        steps=[s for s in steps if s.strip()],
        tags=tags,
        org_url=url or data.get("url") or data.get("orgURL"),
        cuisine=str(cuisine),
    )
    for field, src in (("prepTime", "prepTime"), ("cookTime", "cookTime"), ("performTime", "cookTime"),
                       ("totalTime", "totalTime")):
        if data.get(src):
            recipe[field] = str(data[src])
    if yield_raw and not m:
        recipe["recipeYield"] = str(yield_raw)
    return recipe


def fake_scrape(url: str) -> dict:
    """There is no network here: invent a plausible schema.org Recipe from the URL."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise bad_request("Recipe could not be scraped: invalid URL", "BAD_RECIPE_DATA")
    if "fail" in parsed.netloc or "/fail" in parsed.path:  # handy for testing error paths
        raise bad_request("Recipe could not be scraped", "BAD_RECIPE_DATA")
    last = [p for p in parsed.path.split("/") if p]
    title = re.sub(r"\.\w+$", "", last[-1] if last else parsed.netloc.split(".")[0])
    title = re.sub(r"[-_]+", " ", title).strip().title() or "Scraped Recipe"
    return {
        "@context": "https://schema.org",
        "@type": "Recipe",
        "name": title,
        "description": f"{title}, imported from {parsed.netloc} (fake scrape - the fake server has no network).",
        "url": url,
        "image": None,
        "recipeYield": "4 servings",
        "recipeCuisine": "",
        "keywords": "scraped",
        "recipeIngredient": ["500 g chicken thighs, diced", "1 onion, diced", "2 cloves garlic, crushed",
                             "1 can chopped tomatoes", "2 cups basmati rice", "Salt and pepper, to taste"],
        "recipeInstructions": [{"@type": "HowToStep", "text": t} for t in (
            "Brown the chicken with the onion and garlic.",
            "Add the tomatoes and simmer for 20 minutes.",
            "Serve over rice.")],
    }


@api.post("/recipes/create/url", status_code=201)
async def create_from_url(request: Request) -> str:
    body = await json_body(request)
    url = body.get("url") if isinstance(body, dict) else None
    if not url:
        raise HTTPException(422, "url is required")
    return import_schema_recipe(fake_scrape(url), include_tags=bool(body.get("includeTags")), url=url)["slug"]


@api.post("/recipes/test-scrape-url")
async def test_scrape_url(request: Request) -> dict:
    body = await json_body(request)
    url = body.get("url") if isinstance(body, dict) else None
    if not url:
        raise HTTPException(422, "url is required")
    return fake_scrape(url)


@api.post("/recipes/create/html-or-json", status_code=201)
async def create_from_html_or_json(request: Request) -> str:
    body = await json_body(request)
    raw = body.get("data") if isinstance(body, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        raise HTTPException(422, "data is required and must be a string")
    try:
        data = json.loads(raw)
    except ValueError:
        # Real Mealie would try to scrape HTML here; the fake only understands JSON.
        m = re.search(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', raw, re.S | re.I)
        try:
            data = json.loads(m.group(1)) if m else None
        except ValueError:
            data = None
    if isinstance(data, dict) and isinstance(data.get("@graph"), list):
        data = next((d for d in data["@graph"] if "Recipe" in str(d.get("@type"))), None)
    if isinstance(data, list):
        data = next((d for d in data if isinstance(d, dict)), None)
    if not isinstance(data, dict):
        raise bad_request("Recipe could not be parsed from the supplied data", "BAD_RECIPE_DATA")
    return import_schema_recipe(data, include_tags=bool(body.get("includeTags")))["slug"]


@api.get("/recipes/{slug}")
async def get_recipe(slug: str) -> dict:
    return STATE.recipe_by_slug(slug)


async def _update_recipe(slug: str, request: Request, partial: bool) -> dict:
    recipe = STATE.recipe_by_slug(slug)
    body = await json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(422, "body must be an object")
    if not partial and not (body.get("name") or "").strip():
        raise HTTPException(422, "name is required")
    # Validate everything before mutating so a rejected PUT leaves the recipe untouched.
    updates: dict[str, Any] = {}
    if "recipeIngredient" in body:
        updates["recipeIngredient"] = [STATE.normalise_ingredient(i) for i in body["recipeIngredient"] or []]
    if "tags" in body:
        tags = []
        for t in body["tags"] or []:
            tag = STATE.tags.get(t.get("id") or "") or STATE.tag_by(t.get("slug") or "") or STATE.tag_by(t.get("name") or "")
            if tag is None:
                if not t.get("name"):
                    raise bad_request("Tag does not exist", "NoResultFound")
                tag = STATE.create_tag(t["name"])
            if tag not in tags:
                tags.append(tag)
        updates["tags"] = [dict(t) for t in tags]
    if "recipeInstructions" in body:
        updates["recipeInstructions"] = [
            {"id": s.get("id") or str(uuid.uuid4()), "title": s.get("title") or "", "summary": s.get("summary") or "",
             "text": s.get("text") or "", "ingredientReferences": s.get("ingredientReferences") or []}
            for s in body["recipeInstructions"] or []]
    if "recipeServings" in body:
        updates["recipeServings"] = float(body["recipeServings"] or 0)
    for key in ("description", "recipeYieldQuantity", "recipeYield", "totalTime", "prepTime", "cookTime",
                "performTime", "recipeCategory", "tools", "rating", "orgURL", "lastMade", "nutrition", "settings",
                "notes", "extras"):
        if key in body:
            updates[key] = body[key]
    if (body.get("name") or "").strip() and body["name"].strip() != recipe["name"]:
        updates["name"] = body["name"].strip()
        updates["slug"] = STATE.unique_slug(updates["name"], ignore_id=recipe["id"])  # Mealie re-slugs on rename
    recipe.update(updates)
    recipe["updatedAt"] = recipe["dateUpdated"] = ts()
    return recipe


@api.put("/recipes/{slug}")
async def put_recipe(slug: str, request: Request) -> dict:
    return await _update_recipe(slug, request, partial=False)


@api.patch("/recipes/{slug}")
async def patch_recipe(slug: str, request: Request) -> dict:
    return await _update_recipe(slug, request, partial=True)


@api.delete("/recipes/{slug}")
async def delete_recipe(slug: str) -> dict:
    recipe = STATE.recipe_by_slug(slug)
    del STATE.recipes[recipe["id"]]
    STATE.images.pop(recipe["id"], None)
    for entry in STATE.mealplans.values():  # Mealie nulls the FK on meal plan entries
        if entry["recipeId"] == recipe["id"]:
            entry["recipeId"] = None
    return recipe


@api.put("/recipes/{slug}/image")
async def put_recipe_image(slug: str, request: Request) -> dict:
    recipe = STATE.recipe_by_slug(slug)
    form = await request.form()
    upload = form.get("image")
    if upload is None or isinstance(upload, str) or not form.get("extension"):
        raise HTTPException(422, "multipart fields 'image' (file) and 'extension' are required")
    content = await upload.read()
    if not content:
        raise bad_request("Image is empty", "ValueError")
    if content.startswith(b"\x89PNG"):
        content_type = "image/png"
    elif content.startswith(b"\xff\xd8"):
        content_type = "image/jpeg"
    elif content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        content_type = "image/webp"
    elif content.startswith(b"GIF8"):
        content_type = "image/gif"
    else:
        content_type = upload.content_type or "application/octet-stream"
    STATE.images[recipe["id"]] = (content, content_type)
    recipe["image"] = "".join(random.choices("abcdefghijkmnpqrstuvwxyz23456789", k=4))
    recipe["updatedAt"] = recipe["dateUpdated"] = ts()
    return {"image": recipe["image"]}


# ---- organizers / foods / units -------------------------------------------- #
def _name_filter(request: Request, rows: list[dict]) -> list[dict]:
    search = (request.query_params.get("search") or "").strip().lower()
    return [r for r in rows if search in r["name"].lower()] if search else rows


@api.get("/organizers/tags")
async def list_tags(request: Request) -> dict:
    return paginate(request, order(request, _name_filter(request, list(STATE.tags.values()))))


@api.post("/organizers/tags", status_code=201)
async def create_tag(request: Request) -> dict:
    body = await json_body(request)
    name = (body.get("name") or "").strip() if isinstance(body, dict) else ""
    if not name:
        raise HTTPException(422, "name is required")
    if any(t["slug"] == slugify(name) for t in STATE.tags.values()):
        raise bad_request("Tag already exists", "IntegrityError")
    return STATE.create_tag(name)


@api.get("/organizers/tags/{tag_id}")
async def get_tag(tag_id: str) -> dict:
    tag = STATE.tags.get(tag_id)
    if tag is None:
        raise HTTPException(404, "Not found")
    return {**tag, "recipes": [STATE.summary(r) for r in STATE.recipes.values()
                               if any(t["id"] == tag_id for t in r["tags"])]}


@api.get("/foods")
async def list_foods(request: Request) -> dict:
    return paginate(request, order(request, _name_filter(request, list(STATE.foods.values()))))


@api.post("/foods", status_code=201)
async def create_food(request: Request) -> dict:
    body = await json_body(request)
    name = (body.get("name") or "").strip() if isinstance(body, dict) else ""
    if not name:
        raise HTTPException(422, "name is required")
    if any(f["name"].lower() == name.lower() for f in STATE.foods.values()):
        raise bad_request("Food already exists", "IntegrityError")
    return STATE.create_food(body)


@api.get("/units")
async def list_units(request: Request) -> dict:
    return paginate(request, order(request, _name_filter(request, list(STATE.units.values()))))


@api.post("/units", status_code=201)
async def create_unit(request: Request) -> dict:
    body = await json_body(request)
    name = (body.get("name") or "").strip() if isinstance(body, dict) else ""
    if not name:
        raise HTTPException(422, "name is required")
    if any(u["name"].lower() == name.lower() for u in STATE.units.values()):
        raise bad_request("Unit already exists", "IntegrityError")
    return STATE.create_unit(body)


# ---- parser ---------------------------------------------------------------- #
def _parsed(text: str) -> dict:
    ing = STATE.parse_ingredient(text)
    has_qty, has_food = bool(ing["quantity"]), bool(ing["food"])
    conf = {"quantity": 0.95 if has_qty else 0.2, "unit": 0.9 if ing["unit"] else 0.4,
            "food": 0.85 if has_food else 0.1, "comment": 0.8 if ing["note"] else 0.5, "name": 0.85 if has_food else 0.1}
    conf["average"] = round(sum(conf.values()) / len(conf), 3)
    return {"input": text, "confidence": conf, "ingredient": ing}


@api.post("/parser/ingredients")
async def parse_ingredients(request: Request) -> list:
    body = await json_body(request)
    if not isinstance(body, dict) or not isinstance(body.get("ingredients"), list):
        raise HTTPException(422, "ingredients must be a list of strings")
    return [_parsed(str(text)) for text in body["ingredients"]]


@api.post("/parser/ingredient")
async def parse_ingredient(request: Request) -> dict:
    body = await json_body(request)
    if not isinstance(body, dict) or not isinstance(body.get("ingredient"), str):
        raise HTTPException(422, "ingredient must be a string")
    return _parsed(body["ingredient"])


# ---- meal plans ------------------------------------------------------------ #
ENTRY_TYPES = {"breakfast", "lunch", "dinner", "side"}


def _parse_date(value: Any, field: str) -> date:
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise HTTPException(422, f"{field} must be an ISO date (YYYY-MM-DD)")


@api.get("/households/mealplans")
async def list_mealplans(request: Request) -> dict:
    qp = request.query_params
    start = _parse_date(qp["start_date"], "start_date") if qp.get("start_date") else None
    end = _parse_date(qp["end_date"], "end_date") if qp.get("end_date") else None
    entries = [e for e in STATE.mealplans.values()
               if (not start or e["date"] >= start.isoformat()) and (not end or e["date"] <= end.isoformat())]
    entries.sort(key=lambda e: (e["date"], e["id"]))
    entries = order(request, entries)
    return paginate(request, [STATE.mealplan_out(e) for e in entries])


@api.get("/households/mealplans/today")
async def mealplans_today() -> list:
    today = date.today().isoformat()
    return [STATE.mealplan_out(e) for e in STATE.mealplans.values() if e["date"] == today]


def _validate_entry(body: Any) -> tuple[date, str, Optional[str]]:
    if not isinstance(body, dict):
        raise HTTPException(422, "body must be an object")
    day = _parse_date(body.get("date"), "date")
    entry_type = body.get("entryType") or "breakfast"
    if entry_type not in ENTRY_TYPES:
        raise HTTPException(422, f"entryType must be one of {sorted(ENTRY_TYPES)}")
    recipe_id = body.get("recipeId") or None
    if recipe_id and recipe_id not in STATE.recipes:
        raise bad_request("recipeId does not reference an existing recipe", "ForeignKeyViolation")
    return day, entry_type, recipe_id


@api.post("/households/mealplans", status_code=201)
async def create_mealplan(request: Request) -> dict:
    body = await json_body(request)
    day, entry_type, recipe_id = _validate_entry(body)
    entry = STATE.add_mealplan(day, entry_type, recipe_id, body.get("title") or "",
                               body.get("text") or body.get("note") or "")
    return STATE.mealplan_out(entry)


@api.post("/households/mealplans/random", status_code=201)
async def random_mealplan(request: Request) -> dict:
    body = await json_body(request) if await request.body() else {}
    body = body if isinstance(body, dict) else {}
    # Real Mealie requires {date, entryType}; the client currently sends {} so be lenient.
    day = _parse_date(body["date"], "date") if body.get("date") else date.today()
    entry_type = body.get("entryType") or "dinner"
    if entry_type not in ENTRY_TYPES:
        raise HTTPException(422, f"entryType must be one of {sorted(ENTRY_TYPES)}")
    pool = [r for r in STATE.recipes.values() if any(t["name"] == entry_type for t in r["tags"])] or \
        list(STATE.recipes.values())
    if not pool:
        raise HTTPException(404, "No recipes available")
    entry = STATE.add_mealplan(day, entry_type, random.choice(pool)["id"])
    return STATE.mealplan_out(entry)


def _mealplan(item_id: str) -> dict:
    entry = STATE.mealplans.get(int(item_id)) if item_id.isdigit() else None
    if entry is None:
        raise HTTPException(404, "Not found")
    return entry


@api.get("/households/mealplans/{item_id}")
async def get_mealplan(item_id: str) -> dict:
    return STATE.mealplan_out(_mealplan(item_id))


@api.put("/households/mealplans/{item_id}")
async def update_mealplan(item_id: str, request: Request) -> dict:
    entry = _mealplan(item_id)
    body = await json_body(request)
    day, entry_type, recipe_id = _validate_entry(body)
    entry.update({"date": day.isoformat(), "entryType": entry_type, "recipeId": recipe_id,
                  "title": body.get("title") or "", "text": body.get("text") or ""})
    return STATE.mealplan_out(entry)


@api.delete("/households/mealplans/{item_id}")
async def delete_mealplan(item_id: str) -> dict:
    entry = _mealplan(item_id)
    del STATE.mealplans[entry["id"]]
    return STATE.mealplan_out(entry)


# ---- shopping lists -------------------------------------------------------- #
def _list(list_id: str) -> dict:
    sl = STATE.lists.get(list_id)
    if sl is None:
        raise HTTPException(404, "Not found")
    return sl


@api.get("/households/shopping/lists")
async def list_shopping_lists(request: Request) -> dict:
    lists = order(request, list(STATE.lists.values()))
    return paginate(request, [STATE.list_out(sl, full=False) for sl in lists])


@api.post("/households/shopping/lists", status_code=201)
async def create_shopping_list(request: Request) -> dict:
    body = await json_body(request)
    name = (body.get("name") or "").strip() if isinstance(body, dict) else ""
    return STATE.list_out(STATE.create_list(name))


@api.get("/households/shopping/lists/{list_id}")
async def get_shopping_list(list_id: str) -> dict:
    return STATE.list_out(_list(list_id))


@api.put("/households/shopping/lists/{list_id}")
async def update_shopping_list(list_id: str, request: Request) -> dict:
    sl = _list(list_id)
    body = await json_body(request)
    if isinstance(body, dict) and body.get("name") is not None:
        sl["name"] = str(body["name"])
    sl["updatedAt"] = ts()
    return STATE.list_out(sl)


@api.delete("/households/shopping/lists/{list_id}")
async def delete_shopping_list(list_id: str) -> dict:
    sl = _list(list_id)
    out = STATE.list_out(sl)
    del STATE.lists[list_id]
    for iid in [i["id"] for i in STATE.items.values() if i["shoppingListId"] == list_id]:
        del STATE.items[iid]
    return out


@api.post("/households/shopping/lists/{list_id}/recipe")
async def add_recipes_to_list(list_id: str, request: Request) -> dict:
    sl = _list(list_id)
    body = await json_body(request)
    if not isinstance(body, list):
        raise HTTPException(422, "body must be a list of {recipeId, recipeIncrementQuantity}")
    todo = []
    for entry in body:
        if not isinstance(entry, dict) or not entry.get("recipeId"):
            raise HTTPException(422, "each entry needs a recipeId")
        recipe = STATE.recipes.get(entry["recipeId"])
        if recipe is None:
            raise HTTPException(404, "Recipe not found")
        try:
            scale = float(entry.get("recipeIncrementQuantity", 1) or 1)
        except (TypeError, ValueError):
            raise HTTPException(422, "recipeIncrementQuantity must be a number")
        todo.append((recipe, scale))
    for recipe, scale in todo:
        STATE.add_recipe_to_list(sl, recipe, scale)
    return STATE.list_out(sl)


@api.post("/households/shopping/lists/{list_id}/recipe/{recipe_id}")
async def add_recipe_to_list_legacy(list_id: str, recipe_id: str, request: Request) -> dict:
    sl = _list(list_id)
    recipe = STATE.recipes.get(recipe_id)
    if recipe is None:
        raise HTTPException(404, "Recipe not found")
    body = (await json_body(request)) if await request.body() else {}
    scale = float((body or {}).get("recipeIncrementQuantity", 1) or 1) if isinstance(body, dict) else 1.0
    STATE.add_recipe_to_list(sl, recipe, scale)
    return STATE.list_out(sl)


# ---- shopping items -------------------------------------------------------- #
@api.get("/households/shopping/items")
async def list_shopping_items(request: Request) -> dict:
    qp = request.query_params
    list_id = qp.get("shoppingListId")
    if not list_id and qp.get("queryFilter"):
        m = re.search(r"shoppingListId\s*=\s*\"?([0-9a-fA-F-]{36})", qp["queryFilter"])
        list_id = m.group(1) if m else None
    if list_id:
        items = STATE.list_items(list_id)
    else:
        items = sorted(STATE.items.values(), key=lambda i: (i["shoppingListId"], i["checked"], i["position"]))
    return paginate(request, order(request, items))


def _item(item_id: str) -> dict:
    item = STATE.items.get(item_id)
    if item is None:
        raise HTTPException(404, "Not found")
    return item


def _resolve_ref(body: dict, kind: str, store: dict) -> Optional[dict]:
    ref_id = body.get(f"{kind}Id") or ((body.get(kind) or {}).get("id") if isinstance(body.get(kind), dict) else None)
    if not ref_id:
        return None
    if ref_id not in store:
        raise bad_request(f"{kind}Id does not reference an existing {kind}", "ForeignKeyViolation")
    return store[ref_id]


@api.post("/households/shopping/items", status_code=201)
async def create_shopping_item(request: Request) -> dict:
    body = await json_body(request)
    if not isinstance(body, dict) or not body.get("shoppingListId"):
        raise HTTPException(422, "shoppingListId is required")
    if body["shoppingListId"] not in STATE.lists:
        raise bad_request("shoppingListId does not reference an existing list", "ForeignKeyViolation")
    try:
        quantity = float(body.get("quantity", 1) if body.get("quantity") is not None else 1)
    except (TypeError, ValueError):
        raise HTTPException(422, "quantity must be a number")
    item, created = STATE.add_item(
        body["shoppingListId"], quantity=quantity, unit=_resolve_ref(body, "unit", STATE.units),
        food=_resolve_ref(body, "food", STATE.foods), note=str(body.get("note") or ""),
        checked=bool(body.get("checked")))
    STATE.lists[body["shoppingListId"]]["updatedAt"] = ts()
    return {"createdItems": [item] if created else [], "updatedItems": [] if created else [item], "deletedItems": []}


@api.get("/households/shopping/items/{item_id}")
async def get_shopping_item(item_id: str) -> dict:
    return _item(item_id)


@api.put("/households/shopping/items/{item_id}")
async def update_shopping_item(item_id: str, request: Request) -> dict:
    item = _item(item_id)
    body = await json_body(request)
    if not isinstance(body, dict):
        raise HTTPException(422, "body must be an object")
    if "foodId" in body or "food" in body:
        item["food"] = _resolve_ref(body, "food", STATE.foods)
        item["foodId"] = (item["food"] or {}).get("id")
    if "unitId" in body or "unit" in body:
        item["unit"] = _resolve_ref(body, "unit", STATE.units)
        item["unitId"] = (item["unit"] or {}).get("id")
    for key, cast in (("quantity", float), ("note", str), ("checked", bool), ("position", int)):
        if body.get(key) is not None:
            item[key] = cast(body[key])
    item["display"] = display_ingredient(item)
    item["updatedAt"] = ts()
    return {"createdItems": [], "updatedItems": [item], "deletedItems": []}


@api.delete("/households/shopping/items/{item_id}")
async def delete_shopping_item(item_id: str) -> dict:
    item = _item(item_id)
    del STATE.items[item_id]
    if item["shoppingListId"] in STATE.lists:
        STATE.lists[item["shoppingListId"]]["updatedAt"] = ts()
    return {"message": "Shopping list item deleted", "error": False}


app.include_router(api)


@app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def not_implemented(rest: str, request: Request) -> JSONResponse:
    """Anything the fake doesn't cover: fail loudly so we notice and add it."""
    print(f"[fake-mealie] UNIMPLEMENTED {request.method} /api/{rest}")
    return JSONResponse({"detail": f"fake Mealie does not implement {request.method} /api/{rest}"}, status_code=501)


def create_app(recipes: Optional[int] = None) -> FastAPI:
    """Build the app with fresh seed state (use this from tests: TestClient(create_app()))."""
    global STATE
    STATE = State(recipes)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake Mealie server for local development")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=9925)
    parser.add_argument("--recipes", type=int, default=None, metavar="N",
                        help=f"size of the seeded recipe library (default {len(SEED_RECIPES)}; try 450 for pagination)")
    parser.add_argument("--log-level", default="info")
    args = parser.parse_args()
    import uvicorn

    create_app(args.recipes)
    print(f"[fake-mealie] {len(STATE.recipes)} recipes, {len(STATE.mealplans)} meal plan entries, "
          f"{len(STATE.lists)} shopping list(s) - http://{args.host}:{args.port}  (any non-empty Bearer token works)")
    uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level)


if __name__ == "__main__":
    main()
